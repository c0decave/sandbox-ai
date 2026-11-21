# Signing the axross-mcp-hub catalog for sandbox-ai

`sandbox-ai mcp add` takes a server from a catalog only when `catalog.json.sig`
(and, for Bubblewrap, `rootfs.json.sig`) verifies against a key listed in
`sandbox/mcp-signers`: an SSH signature (`ssh-keygen -Y`) in the namespace
**`sandbox-ai-mcp`** under the principal `mcp@sandbox-ai`.

State on 2026-11-07:

- `sandbox/mcp-signers` trusts exactly one key: `sandbox-ai-mcp`
  (`ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBnhgaPUYUKqV9BD7PmdSluu1jRmEgzGo8NEW6XNisEJ`).
- `https://raw.githubusercontent.com/c0decave/axross-mcp-hub/main/catalog.json.sig`
  does not exist (404). The hub is unsigned: today its servers can only be added
  with `sandbox-ai mcp add NAME --unsigned`.

So the catalog has to be signed, and its signing key has to be trusted.

> Work on the host, never inside a sandbox: an agent must never see a private
> signing key. Only `.pub` files go into a repository.

## What a signature promises

sandbox-ai reads a good signature as: *every server in this catalog was built
from its pinned source, audited, and passed its checks with sandbox-ai's run
lines under Podman and Bubblewrap; the image digests and rootfs hashes in it are
those builds.* sandbox-ai-mcp's `scripts/sign_catalog.py` refuses to sign
otherwise. Sign the hub only when that holds for it as well.

## Way 1: sign the hub with the key sandbox-ai already trusts

No change in sandbox-ai; works with every installed version at once.

```bash
cd axross-mcp-hub
# (run the hub's builds and checks first: see above)
ssh-keygen -Y sign -f ~/.config/sandbox-ai/mcp-signing-key -n sandbox-ai-mcp catalog.json
ssh-keygen -Y sign -f ~/.config/sandbox-ai/mcp-signing-key -n sandbox-ai-mcp rootfs.json   # if the hub has one
git add catalog.json.sig rootfs.json.sig && git commit -m "Sign the catalog" && git push
```

Re-sign after **every** change of `catalog.json` or `rootfs.json`: the signature
covers the exact bytes.

## Way 2: a key of the hub's own

Better separation (a leaked hub key does not sign sandbox-ai-mcp), but it only
works from the sandbox-ai release that ships the new key.

1. Make the key (on the host, keep it outside every workspace):

   ```bash
   ssh-keygen -t ed25519 -f ~/.config/sandbox-ai/axross-hub-signing-key -C "axross-mcp-hub"
   ```

2. Add one line to `sandbox/mcp-signers` in sandbox-ai, with the content of
   `axross-hub-signing-key.pub`. Principal and namespace stay the same; only the
   key and comment are new:

   ```
   mcp@sandbox-ai namespaces="sandbox-ai-mcp" ssh-ed25519 AAAA...(the .pub key)... axross-mcp-hub
   ```

3. Sign as in way 1, with `-f ~/.config/sandbox-ai/axross-hub-signing-key`. The
   namespace must still be `-n sandbox-ai-mcp`, or sandbox-ai refuses the signature.

4. Release sandbox-ai; from that version on `mcp add` takes hub entries.

## Check it

```bash
# Verify by hand, as sandbox-ai does:
ssh-keygen -Y verify -f /path/to/sandbox-ai/sandbox/mcp-signers -I mcp@sandbox-ai \
    -n sandbox-ai-mcp -s catalog.json.sig < catalog.json
# What sandbox-ai sees: every entry of the hub should show "signed"
sandbox-ai mcp catalog
sandbox-ai mcp add NAME            # without --unsigned
```

`Good "sandbox-ai-mcp" signature for mcp@sandbox-ai` is what you want. A
signature that does not verify is refused even with `--unsigned`.

## Optional: a personal trust list

sandbox-ai today trusts only the keys shipped in `sandbox/mcp-signers`; an
update replaces that file. A second list in `~/.config/sandbox-ai/mcp-signers`
(trusted for your machine only, kept across updates) is not built yet; ask for it
if you want to trust a key without a sandbox-ai release.
