# sandbox-ai in a Proxmox LXC container

What an unprivileged LXC container on Proxmox needs for sandbox-ai to start
rootless Podman (or Bubblewrap) in it. The end lists the problems of a first
setup in the order they came, so that a container template can be right from
the start.

A **privileged** container is not needed.

## 1. On the Proxmox host

### More IDs for the container

An unprivileged container has 65536 IDs of its own (0-65535 inside,
100000-165535 on the host). Rootless Podman inside needs a range of 65536
subordinate IDs for the user, within the container's own IDs. So the
container gets more.

In the **host's** `/etc/subuid` and `/etc/subgid`, widen root's line:

```
root:100000:1065536
```

If other containers have an `lxc.idmap` of their own above 165535, the
ranges must not overlap.

### The container's configuration

In `/etc/pve/lxc/<ID>.conf`, with the container stopped:

```
# nested user namespaces, keyrings, FUSE (for Podman)
features: nesting=1,keyctl=1,fuse=1

# more IDs (matching /etc/subuid and /etc/subgid above)
lxc.idmap: u 0 100000 1065536
lxc.idmap: g 0 100000 1065536

# /dev/net/tun for pasta, rootless Podman's network (and for --vpn)
lxc.cgroup2.devices.allow: c 10:200 rwm
lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file
```

The first 65536 IDs stay the same: files already in the container need no
`chown`.

### Kernel modules

A container cannot load kernel modules. The host loads them, also after a
reboot:

```
modprobe -a nft_chain_nat nft_redir tun
printf 'nft_chain_nat\nnft_redir\ntun\n' > /etc/modules-load.d/sandbox-ai.conf
```

The connection proxy needs `nft_chain_nat` and `nft_redir`, pasta needs `tun`.

Then start the container (`pct start <ID>`).

## 2. In the container (part of the template)

### Packages

Debian or Ubuntu with systemd as init (as Proxmox's templates):

```
apt install podman passt uidmap dbus-user-session fuse-overlayfs python3 git curl
# for --backend bwrap also:
apt install bubblewrap firejail nftables slirp4netns
```

- **`passt`** brings pasta, rootless Podman's default network.
- **`uidmap`** brings `newuidmap` and `newgidmap`, with their setuid bit.
- **`dbus-user-session`** brings the bus socket `/run/user/<UID>/bus` of the
  systemd user session. Without it Podman finds no session, even with
  linger.

### The user

```
adduser user
# subordinate IDs for rootless Podman: within the container's own IDs, not the
# user's own ID, at least 65534 (apt needs group 65534 when images are built)
echo 'user:100000:65536' >> /etc/subuid
echo 'user:100000:65536' >> /etc/subgid
# a systemd user session, also without a login (limits, Podman's cgroup manager)
loginctl enable-linger user
```

**Important:** log in as `user` by **SSH** (or on a desktop), not with
`su - user`, `sudo -u user` or after `pct enter`. Only a real login session
sets `XDG_RUNTIME_DIR` and finds the session's bus.

### sandbox-ai

```
curl -fsSL <install.sh URL> | bash      # or: ./install.sh --from <checkout>
# a new terminal (the installer adds ~/.local/bin to ~/.bashrc)
sandbox-ai doctor
sandbox-ai build all
sandbox-ai login claude                 # or codex, opencode
```

## 3. Check

As `user`, in an SSH session:

```
cat /proc/self/uid_map        # 0 100000 1065536 (not 0 100000 65536)
grep user /etc/subuid         # user:100000:65536
ls -l /dev/net/tun            # crw-rw-rw- ... 10, 200
echo "$XDG_RUNTIME_DIR"       # /run/user/1000
ls -l /run/user/1000/bus      # srw-rw-rw- ... (a socket)
podman info                   # no error, no WARN about the session
sandbox-ai doctor             # 0 problem(s)
```

## 4. What went wrong in a first setup

In this order. Every fix is part of the steps above.

| # | Symptom | Cause | Fix |
|---|---|---|---|
| 1 | A start ends with `exit status 125`; `podman info`: `newuidmap: write to uid_map failed: Operation not permitted` | The user's `/etc/subuid` (100000-165535) lies outside the container's IDs (only 0-65535) | Give the container more IDs (section 1) |
| 2 | Way out "a smaller range" (`user:10000:50000`): `doctor` says too few IDs | 50000 are not enough: apt needs group 65534 in an image build | No way out; more IDs from the host |
| 3 | `sandbox-ai build`: `crun ... sd-bus call: Permission denied` | No systemd user session; Podman falls back to cgroupfs for its runs, not for a build's `RUN` steps | `loginctl enable-linger`, `dbus-user-session`, log in by SSH. From sandbox-ai 0.1.30, `sandbox-ai build` uses cgroupfs by itself without a session |
| 4 | Linger set, same error | `dbus-user-session` was missing (no bus socket), or the shell came from `su`/`sudo -u` | Install it, `systemctl restart user@1000`, log in again by SSH |
| 5 | Build: `E: setgroups 65534 failed - setgroups (22: Invalid argument)` | The subordinate range was too small (50000): group 65534 (`nogroup`) did not exist in the user namespace | More IDs for the container, `user:100000:65536` |
| 6 | Build: `Could not open file /var/cache/apt/archives/partial/... - open (13: Permission denied)` | The base image was still unpacked with the old IDs; `podman system migrate` does not change stored images | `podman system reset`, then build again |
| 7 | `podman system reset`: `unlinkat .../storage/overlay/...: permission denied` | The leftovers belong to IDs that are no longer the user's | `sudo rm -rf ~/.local/share/containers/storage`, then `podman system migrate` |
| 8 | A start ends with `exit status 127` | pasta was missing | `apt install passt` |
| 9 | A start: `pasta failed with exit code 1: Failed to open() /dev/net/tun` | The container has no `/dev/net/tun` | Pass the device in (section 1), restart the container |
| 10 | `sandbox-ai login claude`: `Workspace .../auth/.login-claude-.../work lies in ~/.local` | A bug in sandbox-ai up to 0.1.29: its own login sandbox's work folder was refused | Fixed from 0.1.30; before, log in on the host (`claude auth login`) or install a newer one |

What helps to find these from sandbox-ai 0.1.30 on:
- **Start errors:** they show Podman's own message with a hint at the cause,
  not only "exit status 125/127".
- **`sandbox-ai doctor` checks:**
  - that the subordinate IDs lie within the system's IDs, do not overlap and
    are enough;
  - `newuidmap` and `newgidmap`;
  - the user session;
  - pasta with `/dev/net/tun`.

## 5. The app in the container

`sandbox-ai-app` (the graphical front end) needs Qt's libraries in the
container:

```
apt install libegl1 libgl1 libxkbcommon0 libfontconfig1 libdbus-1-3 libxcb-cursor0
```

It reaches your screen in one of three ways:
- **X11 over SSH:** `apt install xauth`, then `ssh -X user@container sandbox-ai-app`.
- **A light desktop in the container:** `apt install xfce4 xrdp`, then an RDP
  client. This is the most robust: terminal windows and the tray work.
- **Qt's own VNC:** `QT_QPA_PLATFORM=vnc:size=1600x1000 sandbox-ai-app`, only
  through an SSH tunnel (`ssh -L 5900:localhost:5900`). It has no password and
  no encryption, and terminal windows do not work with it.

## 6. From another machine

Instead of a desktop in the container, manage it from your own machine over
SSH: `sandbox-ai hosts` keeps the container as a host, and the desktop app
and the web interface (`sandbox-ai web`) show its starts, approvals and
doctor next to your own (README, "Other hosts").

```
sandbox-ai hosts add ct1 user@ct1.lan --description "Proxmox 101"
sandbox-ai hosts test ct1                 # SSH, its sandbox-ai version, its doctor
sandbox-ai hosts run ct1 -- list          # sandbox-ai list in the container
```

The container needs only sshd and sandbox-ai; nothing listens there besides.
