# sandbox-ai quickstart

[Deutsch](QUICKSTART_DE.md) · [Full English documentation](README.md)

Run commands from `sandbox-ai/` as an ordinary user. The default backend needs
working rootless Podman and `pasta`.

## 1. Build the image

```bash
./sandbox-build-image \
  --build-arg CODEX_VERSION=0.164.0 \
  --build-arg CLAUDE_VERSION=2.1.311 \
  --build-arg OPENCODE_VERSION=1.18.32
```

## 2. Check isolation

```bash
./codex-sandbox --workspace "$HOME/src/sandbox-demo" --offline --probe
```

Success produces JSON with `"verified": true`. Missing workspace directories
are created. Existing directories produce a warning and startup continues.
The workspace appears at `/work` inside the sandbox.

## 3. Start an agent

After logging into the chosen agent on the host:

```bash
./codex-sandbox --workspace "$HOME/src/sandbox-demo"
./claude-sandbox --workspace "$HOME/src/sandbox-demo"
./opencode-sandbox --workspace "$HOME/src/sandbox-demo"
```

Existing default auth JSON files are automatically copied privately. Missing
credentials do not prevent startup. `--no-auth` disables automatic copying;
`--auth-file FILE` selects another file. Without `--state-dir`, sandbox HOME
is removed on exit.

A practical Codex file-writing request:

```bash
./codex-sandbox --workspace "$HOME/src/sandbox-demo" \
  -- exec --skip-git-repo-check --ephemeral 'Write Hello into hello.txt and read it back to check.'
cat "$HOME/src/sandbox-demo/hello.txt"
```

Codex defaults to the verified outer sandbox and Firejail, disabling its
additional native OS sandbox with `--sandbox danger-full-access`.
`--workspace-ro` still enforces an external read-only mount. Seeing
`root:root` inside is the expected mapping of the ordinary host user.

OpenCode Cloud (`opencode.ai`) and OpenAI/Anthropic endpoints are allowed for
HTTPS by default. The practically tested free model can be used as follows,
provided it is still offered:

```bash
./opencode-sandbox --workspace "$HOME/src/sandbox-demo" --no-auth \
  -- run --pure --model opencode/glm-5.3-flash-free 'Write Hello into hello.txt and read it back to check.'
```

An OpenCode model catalog must be available; an existing host catalog is
copied. Add `--allow-host models.dev` for online catalog fetching. Check
current prices and availability before later requests.

## 4. Keep state

```bash
./codex-sandbox --workspace "$HOME/src/sandbox-demo" \
  --state-dir "$HOME/.local/state/sandbox-ai/codex"
```

Keep state outside the workspace. Existing state is reused and auth files are
not overwritten. Use separate state directories for concurrent sessions.

## 5. Add mounts and network permissions

```bash
mkdir -p "$HOME/reference" "$HOME/output"
./codex-sandbox --workspace "$HOME/src/sandbox-demo" \
  --mount "$HOME/reference:/mnt/reference:ro" \
  --mount "$HOME/output:/mnt/output:rw"
```

`--allow IP_OR_CIDR` permits all ports/protocols to a destination;
`--allow-host NAME` adds HTTPS. `--no-provider-defaults` removes defaults;
`--offline` disables the external network.

For an explicitly local-only OpenCode model configuration:

```bash
# Replace the example address with the actual model server IP.
./opencode-sandbox --workspace "$HOME/src/sandbox-demo" \
  --local-config "$HOME/.config/opencode/private-model.jsonc" --allow 192.0.2.10
```

The file must configure local providers itself. This mode disables default
cloud permissions and privately copies the configuration. Sandbox
`127.0.0.1` does not refer to the host.

## Alternative: Bubblewrap

With host-installed agents and the documented [requirements](README.md#requirements):

```bash
./codex-sandbox --backend bwrap --workspace "$HOME/src/sandbox-demo" --offline --probe
./codex-sandbox --backend bwrap --workspace "$HOME/src/sandbox-demo"
./opencode-sandbox --backend bwrap --workspace "$HOME/src/sandbox-demo"
```

The launcher uses its own verified Firejail options; it does not additionally
load a user profile. All options: `./codex-sandbox --help`.
Plan without copying or starting processes: `./codex-sandbox --dry-run`.
Practical results: [validation report](evidence/validation-summary.json).
