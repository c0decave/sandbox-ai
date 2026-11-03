---
name: sandbox-ai
description: How to work inside a sandbox-ai sandbox - what is shared with the host, how the network allow-list, the connection proxy, the privacy check of pushes, hardware, displays and sessions work, and which start options or `sandbox-ai net` commands the human must use to unblock something. Use whenever a download, DNS lookup, connection, certificate, git push, device, display or tool fails, before trying any workaround, and when the user asks about the sandbox.
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
- `~/.claude/CLAUDE.md` and Claude's auto memory (`~/.claude/projects/-work/memory/`) belong to this project only and are kept between runs. Keep lasting notes about the
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
  `--allow IP_OR_CIDR`, or a preset: `--allow-github`, `--allow-pypi`, `--allow-gitlab`,
  `--allow-kernel-org`, `--allow-duckduckgo`. `sandbox-ai net show` lists what was added.
- **For the next start**: the same options on the command line or in `.sandbox-ai.json`, or a
  profile: `--profile development` (provider, PyPI, GitHub, OpenAI, Anthropic), `--profile dns`,
  `--profile egress-all`. A server on the host's loopback (local Git server, LLM server) needs
  `--allow-host-port PORT`.
- `sandbox-ai logs` on the host shows what was blocked, with the option that allows it.

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
- Logins for Git come from the human's `--git-token-env`, `--git-token-file` or
  `--git-credentials`; `gh` uses `GH_TOKEN` when it is set. `--git-identity` makes commits carry
  the human's name and e-mail address.

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
| A website or API | `--allow-host NAME` | `sandbox-ai net allow $SANDBOX_AI_RUN --allow-host NAME` |
| GitHub, PyPI, GitLab | `--allow-github`, `--allow-pypi`, `--allow-gitlab` | the same with `sandbox-ai net allow` |
| A port on an address | `--allow-tcp IP:PORT` | `sandbox-ai net allow ... --allow-tcp IP:PORT` |
| A service on the host | `--allow-host-port PORT` | restart needed |
| Everything outbound | `--profile egress-all` (real DNS too) | restart needed |
| A tool rejecting the proxy's CA | `--no-inspect HOST` | restart needed |
| Pushing to GitHub | `--git-token-env https://github.com=VAR` | restart needed |
| A device or the GPU | `--device PATH`, `--gpu KIND` | restart needed |
| A display | `--gui` or `--gui-vnc DIR` | restart needed |
| Files kept in HOME | `--state-dir DIR` or `--session NAME` | restart needed |
| A system package | extend the image, `sandbox-ai build` | restart needed |

Name the exact host, port or path, the error message, and the command; then wait for the human.

sandbox-ai ships this skill and updates it with sandbox-ai. A skill named `sandbox-ai` in one of
your kits replaces it.
