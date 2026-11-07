# sandbox-ai in einem Proxmox-LXC-Container

Was ein unprivilegierter LXC-Container auf Proxmox braucht, damit sandbox-ai
darin rootless Podman (oder Bubblewrap) starten kann. Am Ende steht, welche
Probleme beim ersten Einrichten auftraten, in der Reihenfolge, in der sie kamen.
Mit dieser Liste lässt sich eine Container-Vorlage gleich richtig bauen.

Ein **privilegierter** Container ist nicht nötig.

## 1. Auf dem Proxmox-Host

### Mehr IDs für den Container

Ein unprivilegierter Container hat von Haus aus nur 65536 IDs (0 bis 65535
im Container, 100000 bis 165535 auf dem Host). Rootless Podman braucht im
Container aber selbst einen Bereich von 65536 Unter-IDs für den Benutzer,
und der muss innerhalb der IDs des Containers liegen. Deshalb bekommt der
Container mehr.

In `/etc/subuid` und `/etc/subgid` des **Hosts** die Zeile für root
erweitern:

```
root:100000:1065536
```

Liegen andere Container mit eigenem `lxc.idmap` oberhalb von 165535, dürfen
sich die Bereiche nicht überschneiden.

### Die Container-Konfiguration

In `/etc/pve/lxc/<ID>.conf`, bei gestopptem Container:

```
# Verschachtelte User-Namespaces, Schlüsselbund, FUSE (für Podman)
features: nesting=1,keyctl=1,fuse=1

# Mehr IDs (passend zu /etc/subuid und /etc/subgid oben)
lxc.idmap: u 0 100000 1065536
lxc.idmap: g 0 100000 1065536

# /dev/net/tun für pasta, das Netz von rootless Podman (und für --vpn)
lxc.cgroup2.devices.allow: c 10:200 rwm
lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file
```

Die ersten 65536 IDs bleiben dabei gleich: Dateien, die schon im Container
liegen, brauchen kein `chown`.

### Kernel-Module

Ein Container kann keine Kernel-Module laden. Der Host lädt sie, auch nach
einem Neustart:

```
modprobe -a nft_chain_nat nft_redir tun
printf 'nft_chain_nat\nnft_redir\ntun\n' > /etc/modules-load.d/sandbox-ai.conf
```

`nft_chain_nat` und `nft_redir` braucht der Verbindungs-Proxy, `tun` pasta.

Danach den Container starten (`pct start <ID>`).

## 2. Im Container (gehört in die Vorlage)

### Pakete

Debian oder Ubuntu, mit systemd als Init (wie die Proxmox-Vorlagen):

```
apt install podman passt uidmap dbus-user-session fuse-overlayfs python3 git curl
# nur für --backend bwrap zusätzlich:
apt install bubblewrap firejail nftables slirp4netns
```

- **`passt`** liefert pasta, das Standard-Netz von rootless Podman.
- **`uidmap`** liefert `newuidmap` und `newgidmap`, mit Setuid-Bit.
- **`dbus-user-session`** liefert den Bus-Socket `/run/user/<UID>/bus` der
  systemd-Benutzersitzung. Ohne ihn findet Podman keine Sitzung, auch mit
  Linger.

### Der Benutzer

```
adduser user
# Unter-IDs für rootless Podman: innerhalb der IDs des Containers, ohne die
# eigene UID, mindestens 65534 (apt braucht beim Bauen die Gruppe 65534)
echo 'user:100000:65536' >> /etc/subuid
echo 'user:100000:65536' >> /etc/subgid
# eine systemd-Benutzersitzung, auch ohne Anmeldung (Limits, Podmans cgroup-Manager)
loginctl enable-linger user
```

**Wichtig:** Als `user` per **SSH** (oder am Desktop) anmelden, nicht mit
`su - user`, `sudo -u user` oder nach `pct enter`. Nur eine echte
Login-Sitzung setzt `XDG_RUNTIME_DIR` und findet den Bus der Sitzung.

### sandbox-ai

```
curl -fsSL <install.sh-URL> | bash      # oder: ./install.sh --from <Checkout>
# neues Terminal (der Installer trägt ~/.local/bin in ~/.bashrc ein)
sandbox-ai doctor
sandbox-ai build all
sandbox-ai login claude                 # bzw. codex, opencode
```

## 3. Prüfen

Als `user`, in einer SSH-Sitzung:

```
cat /proc/self/uid_map        # 0 100000 1065536 (nicht 0 100000 65536)
grep user /etc/subuid         # user:100000:65536
ls -l /dev/net/tun            # crw-rw-rw- ... 10, 200
echo "$XDG_RUNTIME_DIR"       # /run/user/1000
ls -l /run/user/1000/bus      # srw-rw-rw- ... (ein Socket)
podman info                   # ohne Fehler und ohne WARN zur Sitzung
sandbox-ai doctor             # 0 problem(s)
```

## 4. Was beim ersten Einrichten schiefging

In dieser Reihenfolge trat es auf. Jede Lösung ist oben schon eingebaut.

| # | Symptom | Ursache | Lösung |
|---|---|---|---|
| 1 | Start endet mit `exit status 125`; `podman info`: `newuidmap: write to uid_map failed: Operation not permitted` | `/etc/subuid` des Benutzers (100000-165535) liegt außerhalb der IDs des Containers (nur 0-65535) | Dem Container mehr IDs geben (Abschnitt 1) |
| 2 | Ausweg „kleinerer Bereich“ (`user:10000:50000`): `doctor` meldet zu wenige IDs | 50000 reichen nicht: apt braucht beim Bauen die Gruppe 65534 | Kein Ausweg; mehr IDs vom Host |
| 3 | `sandbox-ai build`: `crun ... sd-bus call: Permission denied` | Keine systemd-Benutzersitzung; Podman fällt für seine Läufe auf cgroupfs zurück, die `RUN`-Schritte eines Builds nicht | `loginctl enable-linger`, `dbus-user-session`, Anmeldung per SSH. Ab sandbox-ai 0.1.30 baut `sandbox-ai build` ohne Sitzung selbst mit cgroupfs |
| 4 | Linger gesetzt, gleicher Fehler | `dbus-user-session` fehlte (kein Bus-Socket), oder die Shell kam aus `su`/`sudo -u` | Paket installieren, `systemctl restart user@1000`, neu per SSH anmelden |
| 5 | Build: `E: setgroups 65534 failed - setgroups (22: Invalid argument)` | Der Unter-ID-Bereich war zu klein (50000): die Gruppe 65534 (`nogroup`) gab es im User-Namespace nicht | Mehr IDs für den Container, `user:100000:65536` |
| 6 | Build: `Could not open file /var/cache/apt/archives/partial/... - open (13: Permission denied)` | Das Basis-Image war noch mit den alten IDs entpackt; `podman system migrate` ändert gespeicherte Images nicht | `podman system reset`, dann neu bauen |
| 7 | `podman system reset`: `unlinkat .../storage/overlay/...: permission denied` | Die Reste gehören IDs, die nicht mehr dem Benutzer gehören | `sudo rm -rf ~/.local/share/containers/storage`, dann `podman system migrate` |
| 8 | Start endet mit `exit status 127` | pasta fehlte | `apt install passt` |
| 9 | Start: `pasta failed with exit code 1: Failed to open() /dev/net/tun` | Der Container hat kein `/dev/net/tun` | Gerät durchreichen (Abschnitt 1), Container neu starten |
| 10 | `sandbox-ai login claude`: `Workspace .../auth/.login-claude-.../work lies in ~/.local` | Fehler in sandbox-ai bis 0.1.29: der Arbeitsordner der eigenen Login-Sandbox wurde abgelehnt | Behoben ab 0.1.30; vorher auf dem Host anmelden (`claude auth login`) oder neu installieren |

Was ab sandbox-ai 0.1.30 beim Finden hilft:
- **Fehlermeldungen beim Start:** Sie zeigen Podmans eigene Meldung mit einem
  Hinweis zur Ursache, statt nur „exit status 125/127“.
- **`sandbox-ai doctor` prüft:**
  - ob die Unter-IDs innerhalb der IDs des Systems liegen, sich nicht
    überschneiden und reichen;
  - `newuidmap` und `newgidmap`;
  - die Benutzersitzung;
  - pasta mit `/dev/net/tun`.

## 5. Die App im Container

`sandbox-ai-app` (die Oberfläche) braucht im Container die Qt-Bibliotheken:

```
apt install libegl1 libgl1 libxkbcommon0 libfontconfig1 libdbus-1-3 libxcb-cursor0
```

Auf den Bildschirm kommt sie auf einem von drei Wegen:
- **X11 über SSH:** `apt install xauth`, dann `ssh -X user@container sandbox-ai-app`.
- **Ein schlanker Desktop im Container:** `apt install xfce4 xrdp`, dann ein
  RDP-Client. Das ist am robustesten: Terminalfenster und Tray funktionieren.
- **Qts eigenes VNC:** `QT_QPA_PLATFORM=vnc:size=1600x1000 sandbox-ai-app`, nur
  über einen SSH-Tunnel (`ssh -L 5900:localhost:5900`). Es hat weder Passwort
  noch Verschlüsselung, und Terminalfenster gehen damit nicht.

## 6. Von einem anderen Rechner aus

Statt eines Desktops im Container verwalten Sie ihn von Ihrem eigenen Rechner
über SSH: `sandbox-ai hosts` führt den Container als Host, und die Desktop-App
und die Weboberfläche (`sandbox-ai web`) zeigen seine Starts, Rückfragen und
seinen Doctor neben Ihren eigenen (README, „Andere Hosts“).

```
sandbox-ai hosts add ct1 user@ct1.lan --description "Proxmox 101"
sandbox-ai hosts test ct1                 # SSH, seine sandbox-ai-Version, sein Doctor
sandbox-ai hosts run ct1 -- list          # sandbox-ai list im Container
sandbox-ai hosts list                     # seine sandbox-ai-Version, und ob ein Update da ist
sandbox-ai hosts update ct1               # sein eigenes signiertes sandbox-ai-Update (--all: jeder Container)
```

Der Container braucht nur sshd und sandbox-ai; sonst lauscht dort nichts.
Eine Schlüsseldatei, die andere lesen können (ssh lehnt sie ab), machen
`hosts add` und `hosts test` privat und versuchen es dann erneut.

`hosts update` führt das eigene `sandbox-ai update` des Containers aus, so wie
install.sh es dort installiert hat: für den Benutzer aus Abschnitt 2 braucht
das kein root. Eine Systeminstallation (install.sh als root, in /usr/local)
braucht sudo: `hosts update ct1 --sudo`, oder einmal `sandbox-ai hosts set ct1
--update-sudo on`. sudo läuft als `sudo -n`, das nie fragt; will es ein
Passwort, fragt `hosts update` in Ihrem Terminal über `ssh -t` (die App und
die Weboberfläche in einem Terminal-Tab), oder eine sudoers-Regel für genau
diesen Befehl (NOPASSWD) erspart die Frage. Eine Anmeldung als root
(`root@ct1`) braucht beides nicht.
