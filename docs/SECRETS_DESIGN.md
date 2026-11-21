# Secrets at rest: the primary password

`sandbox-ai secrets` keeps API keys, tokens, VPN keys and passwords. Where a system keyring answers (Secret Service
through `secret-tool`, the macOS login Keychain through `security`) they live there, encrypted by the system. Where
none does - a server, a container, a headless machine, `SANDBOX_AI_SECRETS=file` - they live in private files
(`0600`) in `~/.local/share/sandbox-ai/secrets`. Until 0.1.98 those files were plain text. Since then they are
encrypted with a **primary password**, as a browser's password manager has one: without it the files cannot be
read; with it, typed once per session, everything works as before.

This document says what the construction is, why, what it protects from, and what it does not.

## What it is for (threat model)

Protected, once a primary password is set and the store is locked:

| Who | Gets |
|---|---|
| someone with a copy of the disk, a backup, a snapshot, a synced HOME, a stolen laptop that is off | file names (the secrets' names), sizes rounded to 64 bytes, the header - no value |
| another program of your user that reads files (an editor's indexer, a `tar` of HOME, a `grep -r`, an agent that was given your HOME by mistake) | the same |
| root on the machine, while locked and from then on | the same (root that waits for the next unlock gets everything: see below) |
| someone who changes a file (swaps two secrets, puts an older text in, flips bits) | detected: the secret is refused as damaged or changed - except an older text of the *same* secret (no rollback protection) |

Not protected - said plainly:

- **While unlocked**, every process of your user can ask the agent to open a secret (as with `ssh-agent`, the
  keyring or the Keychain once they are unlocked). The agent never gives out the key itself, refuses other users
  (peer credentials from the kernel), disables core dumps and, on Linux, `ptrace` and `/proc/PID/mem` by other
  processes of the user (`PR_SET_DUMPABLE`). Root reads its memory anyway.
- **A weak password.** The files can be attacked offline; the only cost is the key derivation (below). Eight
  characters is the least it takes, not a recommendation: use a passphrase.
- **Names.** A secret's name is its file's name (`GH_PUSH`, `MULLVAD_ACCOUNT`); the lists that name them
  (`git-logins.json`, `llm.json`, VPN profiles) are plain. A browser's password manager leaves the sites' names
  readable in the same way.
- **What was on the disk before.** Setting the password writes over each plain file before it goes, which helps
  on a file system that writes in place (ext4, XFS without reflinks). On copy-on-write file systems (btrfs, ZFS,
  APFS), with snapshots, on SSDs (the flash translation layer keeps old pages until it
  collects them), in journals and in backups the old plain text may survive. **A secret that lay in a plain file
  on a disk you do not fully trust should be replaced at its issuer** (a new token), not only re-encrypted.
- **A secret after a sandbox got it.** A start that is given a secret hands it to its connection proxy or to the
  agent's environment for as long as it runs (the run's directory, `0600`, removed with the run). That is what
  secrets are for; the primary password protects what is at rest.
- **Rollback, deletion, replacement.** Someone who can write the files can delete a secret, put back an older
  encrypted text of the same name, or put a plain file of their own in its place (a plain file beside the header is
  read, with the warning, so that an interrupted encryption never locks you out). Nothing here detects that; it
  takes your user's write access, which can do worse.
- **A changed sandbox-ai.** Whoever can replace the program can ask for the password itself.

## Construction

Format version 1. Two kinds of file in the store's folder:

```
.primary.json   {"format": "sandbox-ai secrets", "version": 1, "id": 32 hex,
                 "kdf": {"name": "scrypt", "n": 131072, "r": 8, "p": 1, "salt": base64(32 bytes)},
                 "key": base64(seal(KEK, aad = "primary\0" + {"id","kdf","version"}, DATA KEY)), "created": ...}
NAME            "sandbox-ai-secret-v1:" + base64(seal(DATA KEY, aad = "secret\0" + NAME, value)) + "\n"
```

**Keys.** The *data key* is 64 random bytes (`os.urandom`), made once when the password is set. It encrypts the
secrets. The *key-encryption key* (KEK) is derived from the password and the store's own random salt; it only
seals the data key in the header. Opening the header with a password either yields the data key or fails its
authentication tag: that is the verifier for a wrong password, and there is no separate password hash to attack.
Because the header's id and derivation parameters are part of what is authenticated, a header whose parameters were
changed does not open.

A changed password seals the same data key under a new KEK and a new salt: one atomic file replace, no secret is
rewritten, an agent stays unlocked. The price: someone who has an **old copy of the header and the old password**
can still open secrets written later. If the old password leaked together with the files, `secrets password
remove` followed by `secrets password set` makes a new data key (and replace the secrets themselves).

**Key derivation.** `hashlib.scrypt` with n = 2^17, r = 8, p = 1, a 32-byte salt, 64 bytes out: 128 MiB of memory
and about 0.3 s of one core per guess on a current desktop (OWASP's 2023 minimum for scrypt is n = 2^17, r = 8,
p = 1). The memory is the point: it is what keeps a guess expensive on GPUs and ASICs. The parameters are in the
header, so they can be raised later without a new format; they are bounded when read (n a power of two up to 2^22,
r up to 32, p up to 16, and 128 * n * r - the memory of one attempt - at most 1 GiB), so a changed header cannot make a
command take gigabytes. A Python built without scrypt
(LibreSSL builds) writes PBKDF2-HMAC-SHA-512 with 1.2 million rounds instead and says so in the header; a store
written with scrypt is refused there with the reason, not silently read some other way. The password counts in its
Unicode NFKC form, so it is the same from a Linux and a macOS keyboard.

**Sealing** (`seal` / `unseal` in `sandbox/secret_store.py`). `secret_store.py` is standard library only, so that
every command that reads a secret (the launcher, in every packaging: checkout, pip, the single-file `.pyz`, the
AppImage, the macOS app; on a remote host with nothing but Python) needs no extra package. Python's standard
library has no AES and no ChaCha20. It has HMAC and SHA-2. So:

```
nonce              24 random bytes per message
K_enc, K_mac       HMAC-SHA-512(key, "sandbox-ai secrets v1 encrypt\0" + nonce),
                   HMAC-SHA-512(key, "sandbox-ai secrets v1 authenticate\0" + nonce)
padded             4-byte length + plaintext, zero-padded to a multiple of 64 bytes
ciphertext         padded XOR ( HMAC-SHA-512(K_enc, counter_0) | HMAC-SHA-512(K_enc, counter_1) | ... )
tag                HMAC-SHA-512(K_mac, len(aad) as 8 bytes + aad + nonce + ciphertext), first 32 bytes
sealed             nonce | ciphertext | tag
```

That is a pseudo-random function in counter mode as a stream cipher, then encrypt-then-MAC with a separate key:
the generic composition that is secure when the cipher is IND-CPA and the MAC is unforgeable (Bellare and
Namprempre). HMAC-SHA-512 is a PRF under the usual assumption on SHA-512's compression function; a PRF in counter
mode is IND-CPA as long as no (key, counter) pair repeats, and here every message has its own keys from a 192-bit
random nonce and a counter that starts at 0. The tag is checked in constant time (`hmac.compare_digest`) before
anything is decrypted; it covers the associated data with its length, so the split between name and text cannot be
moved. The padding hides a token's exact length (which often tells its issuer).

Why not something else:

- *AES-GCM or ChaCha20-Poly1305 from `cryptography` / PyNaCl*: the better-known primitives, and not in the standard
  library. They are not installed with Python on macOS or on a minimal Debian; requiring them would break "a host
  needs sshd and sandbox-ai" and the single-file package. Shipping a pure-Python AES would be a hand-written block
  cipher with timing side channels: worse than composing HMAC.
- *The `openssl` command* (present on Linux and macOS): `openssl enc` has no authenticated mode on the command line
  (GCM is refused there), its password handling goes through arguments, environment or files, and macOS ships
  LibreSSL with other options. A key or password would have to cross a process boundary for every secret.
- *SHAKE-256 or BLAKE2 as the stream*: also sound and standard library; HMAC-SHA-512 was taken because its PRF
  claim is the oldest and best studied, and speed does not matter for texts of at most 8 KiB.

The format is pinned by a known-answer test (`tests/test_secrets_primary_password.py`): version 1 never changes
silently. A better primitive later is version 2 beside it.

## Setting, changing, removing

```sh
sandbox-ai secrets password set       # typed twice, never shown; encrypts what is there; unlocks for 8 hours
sandbox-ai secrets password change    # the current one, then the new one twice
sandbox-ai secrets password remove    # the current one; plain files again (said, with what that means)
```

For scripts: `--password-stdin` (one line; `change`: the current one, then the new one) or `--password-fd N`.
There is no option that takes a password as an argument and no environment variable: both show in `ps` and are
passed on to child processes.

- **The first secret needs it.** `sandbox-ai secrets set NAME` with the file store and no primary password asks on
  a terminal to set one first (twice), then for the value. Without a terminal it stops and names the command.
  Keeping plain files is possible and is a choice someone has to make: `secrets set NAME --plain`, or
  `SANDBOX_AI_SECRETS_PLAIN=1` for a machine with nobody to ask (a CI runner, a service that must start unattended).
- **Existing plain stores keep working.** Nothing is refused: a secret read from a plain file works as before, and
  the command says once: *warning: the secret NAME is kept in a plain file, not encrypted ... Set a primary
  password: sandbox-ai secrets password set*. `sandbox-ai secrets backend`, `secrets status` and `sandbox-ai
  doctor` (check "Secrets") say which protection is in force.
- **Setting it** writes the header first, then encrypts each plain file: the encrypted text goes to a new `0600`
  file that is synced, renamed over the old one (atomic), and the old file's blocks are then written over through
  a descriptor that was opened before the rename. No temporary file ever holds plain text. If the power goes
  between two files, the rest are still plain beside the header: they stay readable (with the warning), and the
  next `secrets unlock` encrypts them. The same happens to a plain file an older sandbox-ai or a restore put there.
- **Removing it** decrypts every secret first (a damaged one stops it before anything changed), writes them back
  as plain files, writes over and removes the header, and ends the agent.
- **There is no recovery.** No hint, no second key. A forgotten password means: delete the secrets (`secrets
  delete NAME`, or the folder) and keep them again.

## Unlocking

One prompt per session.

```sh
sandbox-ai secrets unlock [--for 30m|8h|2d] [--idle 30m]    # default: 8 hours
sandbox-ai secrets lock
sandbox-ai secrets status [--json]
```

`unlock` derives the key, checks it, and starts a small **agent**: a process of your user that holds the data key
in memory and nothing else. It listens on a Unix socket (`0600`) in a folder that is yours alone (`0700`):
`$XDG_RUNTIME_DIR/sandbox-ai/secrets/`, else `~/Library/Caches/sandbox-ai/secrets/` (macOS), else
`$TMPDIR/sandbox-ai-UID/secrets/` - the places `sandbox-ai hosts` keeps its SSH connections. No sandbox gets them
unless you mount one yourself, and then the start warns that a socket there reaches what runs as you (the
launcher's check of the user's socket folders covers them). The agent

- gets the key through a pipe on its standard input - not on its command line, not in its environment;
- answers only connections whose peer, by the kernel's word (`SO_PEERCRED`, `LOCAL_PEERCRED` on macOS), is its own
  user; a peer it cannot identify is refused; a client in turn checks the folder, the socket and the peer;
- opens and seals single secrets and reports its state; it has no request that returns the key;
- ends after `--for`, after `--idle` without a secret being read or kept, on `secrets lock`, and when a new
  `unlock` replaces it; its time also runs by the wall clock, so a laptop that slept does not stay unlocked longer;
- writes no core file and is not dumpable (Linux);
- binds its socket again when the folder went away and came back: on a host managed over SSH the login session's
  `XDG_RUNTIME_DIR` is removed when the last SSH command ends, and the unlock is meant to outlive that.

Who asks:

- **A terminal.** A command that needs an encrypted secret while locked (a start with `--secret-env`, a Git
  login, a VPN profile, `llm check` ...) asks *Primary password* on its terminal - three attempts, Enter alone
  skips - and starts the agent for 8 hours, so the next command does not ask again. Without a terminal it stops
  with: *the secrets are locked (encrypted with the primary password): sandbox-ai secrets unlock*.
- **The desktop app.** Credentials, Secrets says what protects the secrets of the host chosen and has one button
  for the step that is due: *Set password…* (typed twice), *Unlock…*, *Lock* (also in the list's context menu).
  Keeping a secret in a store that is locked or has no password yet asks for the password first, in a hidden
  field. The app keeps no key itself: it runs `sandbox-ai secrets unlock --password-stdin`, so the terminals it
  opens and the starts it makes use the same agent. A start in one of its terminals asks there, as any terminal.
- **The web interface.** Page Secrets: the same line and step (`GET /api/hosts/H/vault`, `POST .../vault/unlock`,
  `/vault/lock`, `/vault/new`). Only for a logged-in session (password and TOTP, CSRF token, same origin), only
  over the service's HTTPS (it speaks nothing else), the password in a field that starts hidden and is not
  remembered. Wrong passwords are slowed down per host by the service (three at once, then 5 s doubling to 5
  minutes) on top of the store's own count; every attempt is in the audit log, the password never (the field
  `password` is always left out). Changing and removing the password are the command line's.
- **Another host** (`sandbox-ai hosts`, the app or the web interface on another machine): the same commands there,
  over the SSH connection that exists, the password on the remote command's standard input - never in the remote
  command line (which other users of that host could see in `ps`). On a terminal: `sandbox-ai hosts run HOST --
  secrets unlock` asks on that host's terminal; from a script: `... | sandbox-ai hosts run HOST -- secrets unlock
  --password-stdin`. The key never leaves the host whose secrets it opens.

**Wrong passwords.** Each guess costs the key derivation (0.3 s, 128 MiB). The store counts wrong attempts in
`.attempts` beside the header, whatever way they come in (a terminal, the app, the web interface, SSH): three at
once, then 1, 2, 4 ... up to 300 seconds between attempts, until a right one. One attempt is checked at a time
(a lock file, `.attempts.lock`): guesses sent at once are attempts in a row, not that many checks against one
count. That slows guessing *through*
sandbox-ai (a web session, a remote shell with limited rights); it is no protection for a copy of the files, where
only the password's strength and scrypt count.

## What else sandbox-ai writes, and whether it is encrypted

Everything below was looked at for this change (every `0600` write in `sandbox/` and the app).

| What | Where | Now |
|---|---|---|
| Secrets: API keys, tokens, Git and publish logins' tokens, LLM servers' keys and header secrets, WireGuard private and preshared keys, Mullvad account numbers, Tailscale auth keys, OpenVPN user:password, Tor bridges, SMB passwords | keyring / Keychain, else `~/.local/share/sandbox-ai/secrets` | **encrypted** (by the system, or by the primary password) |
| `git-logins.json`, `publish-logins.json`, `identities.json`, `llm.json`, `hosts.json`, MCP servers, VPN profiles' `*.json`, project files | `~/.config/sandbox-ai` | no credential in them: servers, users and the *names* of secrets |
| Web interface logins | `~/.config/sandbox-ai/web/users.json` | passwords as scrypt hashes (not reversible). **TOTP secrets plain** (`0600`): the service must check a login before anyone is there to unlock anything; encrypting them with the primary password would make the unlock step need the very login it protects. A TOTP secret alone logs nobody in (the password is needed too) |
| Web interface TLS key | `~/.config/sandbox-ai/web/key.pem` | plain (`0600`), as every server's key that must start unattended; `web cert --renew` makes a new one |
| OpenVPN profile | `~/.config/sandbox-ai/vpn/NAME.ovpn` | **encrypted** as a sealed file (below), with the client key and certificates it names taken in; opened only in memory when a VPN starts |
| Push tokens typed in the wizard | secrets `GIT_TOKEN_HOST`; before 0.1.98 `~/.config/sandbox-ai/tokens/HOST` | **encrypted** as secrets; an older token file is replaced by a reference to its secret (below), so `--git-token-file URL=FILE` in existing project files keeps working |
| A token file of your own (`--git-token-file URL=FILE` elsewhere) | where you keep it | yours, plain; read as before, with a warning once per start (`--git-token-secret` keeps it encrypted) |
| Agents' logins (`sandbox-ai login`) | `~/.local/share/sandbox-ai/auth`, and in every kept HOME under `~/.local/state/sandbox-ai` | **plain** (`0600`), in each agent's own format: the agent in the sandbox reads and rewrites them while it runs (token refresh), and sandbox-ai copies them back. Encrypting the store while the same token lies in every kept HOME would protect nothing; it needs kept HOMEs encrypted at rest, which is a design of its own |
| A run's credentials (the proxy's keys, Git logins given during the run, SMB credentials file, VPN state) | the run's directory under `$XDG_RUNTIME_DIR` or the state folder | plain (`0600`) while the run lives, removed with it: the proxy and the VPN container read them without anyone present |
| Canary tokens | state folder | not credentials: bait, meant to be found |
| SSH keys | `~/.ssh` | yours; sandbox-ai only fixes their permissions |
| Logs (launcher, connections, LLM traffic, web audit) | state folder | no secret by construction: values read from the store are registered for redaction, bodies are masked, the audit log leaves secret fields out |

## Sealed files: OpenVPN profiles, and the wizard's push tokens

Two credentials lay beside the store in plain `0600` files until 0.1.98: OpenVPN profiles (multi-line, up to 1 MiB,
with the client's key - more than a secret's one line of 8 KiB) and the push tokens the wizard wrote into files.

**OpenVPN profiles are sealed as files**, with the construction above and no second one:

```
NAME.ovpn       "sandbox-ai-sealed-file-v1 " + KEYREF + "\n" + base64(seal(KEY, aad, file bytes)) + "\n"
aad             "file\0" + "vpn/NAME.ovpn" + "\0" + KEYREF
KEYREF          "primary:" + the store's id    (KEY: the store's data key)
                "system:SANDBOX_AI_FILE_KEY"   (KEY: 64 random bytes kept in the keyring or the Keychain)
```

The file's name and the key's reference are bound in: a profile copied to another name, or given another key's
reference, is refused as changed. The padding hides the size to 64 bytes. While the agent holds the key, it seals and
opens files too (`seal_file`, `open_file`; only for its own store's reference, the text in base64 over the socket,
up to 1 MiB) - the key never leaves it.

- **Opened only in memory.** `vpn check`, a start and an MCP server's or a team member's VPN read the profile into
  the launcher's memory and hand it to the VPN container on its standard input, which writes it into `/run` of the
  container: a tmpfs. No plain copy goes to the host's disk. A locked store: asked on a terminal, else the usual
  *the secrets are locked ...: sandbox-ai secrets unlock* (the app and the web interface show that message; their
  Secrets pages unlock).
- **New profiles** (`vpn add --openvpn`) are sealed as a new secret is kept: a store without a primary password asks
  for one on a terminal, else stops and names `sandbox-ai secrets password set`; `SANDBOX_AI_SECRETS_PLAIN=1` is the
  choice of plain files. `vpn remove` writes the file over before it removes it.
- **The keyring and the Keychain** have no primary password. A profile is not left plain there: it is sealed with a
  random 64-byte key kept as the secret `SANDBOX_AI_FILE_KEY` (made once, under the store's lock, so two first uses
  make one key). Whoever has a copy of the disk without your keyring's login gets nothing; whoever runs as you while
  the keyring is open gets the profile, as they get every secret there. The key is a secret like the others: `secrets
  delete SANDBOX_AI_FILE_KEY` is refused while a sealed file needs it; a login without the keyring (SSH without the
  desktop's session) cannot open the profiles, as it cannot read the keyring's secrets - the message says so.
- `secrets password remove` writes the profiles sealed with the store's key back as plain files, with the secrets.

**Push tokens become secrets.** The wizard (the command line and the app) keeps a typed token as the secret
`GIT_TOKEN_HOST` (`GIT_TOKEN_GITHUB_COM`) and writes `--git-token-secret URL=NAME` into the project file. An older
token file in `~/.config/sandbox-ai/tokens/` becomes a reference: its first line `sandbox-ai-token-moved-to-secret
NAME`, then a comment for whoever opens it. `--git-token-file URL=FILE` pointing at it reads the secret (locked: the
unlock message), and says once - remembered in the file - that the project file can name `--git-token-secret
URL=NAME` instead (`sandbox-ai wizard edit` offers the secret in its place). A secret of that name with another
token gets a name of its own (`..._2`). A token file anywhere else is yours: read as before, never moved, with a
warning once per start.

**Migration.** Plain profiles and token files are moved

- when the primary password is set (`secrets password set`, also the first secret's prompt) and at every unlock,
  as the store's own plain files are: the result is said (`moved under the primary password: vpn/work.ovpn
  (encrypted), tokens/github.com (now the secret GIT_TOKEN_GITHUB_COM)`);
- at their first use when that needs nobody to type anything - the keyring, or the store unlocked. Locked, a plain
  profile or token is read as it is (it is plain already), with a warning, and moved later.

Each is replaced atomically (a new `0600` file, synced, renamed over the old one) and the old file's blocks are
written over through a descriptor opened before - the same `replace_wiping` as the secrets; no temporary file holds
plain text. The limits of that are those above: copy-on-write file systems, snapshots, SSDs, journals and backups
may keep the old text. A profile's key or a token that lay in a plain file on a disk you do not fully trust should
be replaced at its issuer. `secrets status` (`plain_files` in `--json`) and the doctor's check "Secrets" list what
is still plain.

## Files

- `sandbox/secret_store.py`: the construction, the store, the agent, the commands, sealed files and moved tokens.
- `sandbox/vpn.py` (`openvpn_config`, `add_openvpn`, `remove`), `sandbox/agent_sandbox.py` (`read_token_file`),
  `sandbox/wizard.py`: their users.
- `sandbox/doctor.py`: the check "Secrets".
- `sandbox/webui.py`, `sandbox/web/app.js`: the unlock step of the web interface.
- `projects/sandbox-ai-app/sandbox_ai_app/credentials_page.py`, `backend.py`: the app.
- `tests/test_secrets_primary_password.py` (the construction's known answer, no plain text left, wrong password,
  tampering, the agent's socket and other users, nothing in arguments, environments, logs), `tests/test_web.py`
  (the unlock step), `projects/sandbox-ai-app/tests/test_primary_password.py`; `tests/test_secrets_sealed_files.py`
  (sealed files and moved tokens: the construction, the agent, the keyring's key, migration with no plain text left
  - a canary grepped across the whole temporary HOME, the old blocks written over -, locked, old project files, and
  `./sandbox-ai` end to end), `projects/sandbox-ai-app/tests/test_wizard_token_secret.py`.
