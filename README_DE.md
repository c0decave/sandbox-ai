# sandbox-ai

[English](README.md) · [Schnellstart-Tutorial](QUICKSTART_DE.md) · [Lernvideo](docs/video/README.md)

![Die Desktop-App von sandbox-ai: eine Sandbox auf der Tafel zusammengestellt](docs/screenshots/board.png)

**Eine Zentrale für KI-Agenten: auf diesem Rechner oder auf jedem Host, den Sie verwalten, abgeschottet, beobachtet,
und gefragt, bevor etwas hinausgeht.**

1. **Jeder Agent an einem Ort.** Claude Code, Codex und OpenCode, jeder mit eigenem Login, eigenen Plugins und
   MCP-Servern, gestartet von der Kommandozeile, aus der Desktop-App oder im Browser.
2. **Zwei Sandbox-Schichten, geprüft.** Rootless Podman oder Bubblewrap, darin Firejail; der Lauf bricht ab, wenn
   Namespaces, Capabilities, NoNewPrivs und Seccomp nicht bestätigt sind. Kein stiller Rückfall.
3. **Nichts geht ungefragt hinaus.** Netzwerk standardmäßig zu, Freigaben pro Host, und für alles andere eine Frage
   auf Ihrem Desktop: einmal, für den Lauf oder immer für das Projekt.
4. **Geheimnisse betreten die Sandbox nie.** API-Schlüssel, Git- und Registry-Logins setzt der Proxy erst auf der
   Leitung ein; der Agent sieht nur Platzhalter.
5. **Jeder Push geprüft.** Jedes `git push` und jede Veröffentlichung wird auf Geheimnisse und private Daten
   geprüft, bevor der Server sie sieht.
6. **Sie sehen alles.** Verbindungen, Prompts, Tool-Aufrufe, Dateien und Prozesse je Lauf, live, mit dem Prozess
   hinter jeder Verbindung; Köder-Dateien melden, wenn ein Agent nach Zugangsdaten sucht.
7. **Setups grafisch bauen.** Teile auf die Tafel ziehen (Arbeitsordner, Netzwerk, MCP-Server, LLM-Server, VPN,
   Geräte, Limits), Sicherheitswert und genaue Kommandozeile sehen, als eigene Vorlage speichern.
8. **Viele Rechner, ein Fenster.** Andere Hosts und LXC-Container über SSH verwalten: ihre Sandboxen, Rückfragen,
   Terminals und Updates, aus der App oder der Weboberfläche mit Passwort und TOTP.
9. **Teams, Helfer und lange Läufe.** Lösbare Sitzungen, mit denen Sie sich von überall verbinden, Agenten-Teams mit
   Aufgabentafel, lokale oder entfernte LLMs, Tor- und VPN-Profile, GPUs und USB-Geräte nach Regel.
10. **Sicher als Vorgabe, streng auf Wunsch.** Sicherheitsmodi von `lax` bis `paranoid`, Regelsätze, Taint des Laufs
    und die Richtlinie des Administrators, während der Agent arbeitet wie immer.

## Ein Blick in die App

| | |
|---|---|
| ![Start](docs/screenshots/launch.png) **Start**: Agent und Optionen wählen, sehen, was offen sein wird | ![Tafel](docs/screenshots/board.png) **Tafel**: eine Sandbox als Teile zum Ziehen und Verbinden, mit Sicherheitswert |
| ![Sandboxen und Sitzungen](docs/screenshots/sandboxes.png) **Sandboxen & Sitzungen**: alles, was läuft, egal wie gestartet | ![Freigaben](docs/screenshots/approvals.png) **Freigaben**: was die Agenten fragen, mit einem Klick beantwortet |
| ![Verbindungen](docs/screenshots/connections.png) **Verbindungen**: jede Anfrage mit dem Prozess dahinter | ![Aktivität](docs/screenshots/activity.png) **Aktivität**: Prompts, Tool-Aufrufe, Dateien und Netzwerk je Lauf |
| ![Terminals](docs/screenshots/terminals.png) **Terminals**: in jede laufende Sitzung in einem Tab | ![Kits und Plugins](docs/screenshots/kits.png) **Kits & Plugins**: Skills, Plugins und Hooks für jeden Agenten |
| ![Hosts](docs/screenshots/hosts.png) **Hosts**: andere Rechner über SSH im selben Fenster | ![Hilfe](docs/screenshots/help.png) **Hilfe**: jede Seite und jedes Konzept erklärt, auf Deutsch und Englisch, mit Touren |
| ![Benachrichtigungen](docs/screenshots/notifications.png) **Benachrichtigungen** im Design: Warnungen und Fehler in seinen Farben | ![Weboberfläche](docs/screenshots/web.png) **Weboberfläche**: dasselbe im Browser, mit Passwort und TOTP |

Die Bilder zeigen gestellte Demodaten (ein Projekt in einem temporären Ordner und ein Lauf mit zwei offenen Fragen).

### In fünf Minuten loslegen

```bash
curl -fsSL https://github.com/c0decave/sandbox-ai/releases/latest/download/install.sh | bash   # oder AppImage / .app
sandbox-ai doctor                      # prüft den Host: Podman oder Bubblewrap, Firejail, nftables
sandbox-ai build claude                # das Image des Agenten (claude, codex oder opencode)
sandbox-ai claude -w ~/src/meinprojekt # der Agent in seiner Sandbox; Ctrl-b d löst, er arbeitet weiter
sandbox-ai-app                         # die Desktop-App: Start, Tafel, Freigaben, Terminals, Hilfe
```

In der App: Projektordner öffnen, auf **Start** den Agenten wählen (oder auf der **Tafel** ein Setup
zusammenziehen), **In der App starten** drücken und unter **Freigaben** beantworten, was der Agent fragt.
**Hilfe → Touren** führt durch den ersten Start, die eigene Vorlage, Freigaben und den Rest.


Linux-Starter, die **Codex**, **Claude Code** und **OpenCode** in zwei
Sandbox-Schichten ausführen, mit standardmäßig gesperrtem Netz:

```text
Host-Benutzer → rootless Podman-Pod oder Bubblewrap → Firejail → Agent
```

`sandbox-ai claude|codex|opencode` führen den Host-Starter `sandbox/agent_sandbox.py` aus; in
die Sandbox wird nur `sandbox/runtime.py` eingeblendet, schreibgeschützt, als
deren Bootstrap. Vor dem Agentstart prüft ein Guard, dass Firejail
zusätzliche Mount- und PID-Namespaces angelegt, alle fünf Capability-Mengen
geleert sowie NoNewPrivs und Seccomp aktiviert hat; jede fehlgeschlagene
Prüfung bricht ab. Ausgehender Verkehr ist per nftables auf den Provider des
Agenten beschränkt, solange Sie ihn nicht erweitern.

### Warum

Ein Agent mit Shell-Zugriff führt aus, was er für eine gute Idee hält, mit den
Rechten Ihres Benutzers: Er kann `~/.ssh` lesen, zu jedem Remote pushen oder
Ihren Code an jeden Host schicken. sandbox-ai gibt ihm nur das
Projektverzeichnis und die Netzziele, die Sie nennen, und macht das prüfbar
statt nur versprochen:

- **Geprüfte Einschränkung.** Der Lauf bricht ab, wenn der Guard Namespaces,
  Capabilities, NoNewPrivs und Seccomp nicht bestätigen kann; kein stiller
  Rückfall.
- **Standardmäßig gesperrtes Netz** mit Freigaben je Host und ein
  Verbindungs-Proxy, der jede Anfrage protokolliert und jeden `git push` über
  HTTP(S) auf Geheimnisse und private Daten prüft, bevor der Server ihn sieht.
- **Fragen statt Scheitern.** Eine Verbindung, die die Optionen nicht erlauben,
  wird zur Frage auf Ihrem Desktop (`sandbox-ai approvals`), mit der fragenden
  Sandbox; Sie erlauben sie einmal, für den Lauf oder für das Projekt.
- **Ihr Agent, wie er ist.** Claude Code, Codex und OpenCode mit eigenen
  Logins, Plugins und MCP-Servern, im Terminal, in einer abkoppelbaren
  tmux-Session oder in der [Desktop-App](#desktop-app).

Nehmen Sie etwas anderes, wenn Sie Schutz vor Kernel-Exploits brauchen (eine
VM), unter Windows arbeiten oder weder rootless Podman noch Bubblewrap nutzen
können. Unter macOS laufen die Sandboxes in einer rootless `podman machine`,
bisher mit [Einschränkungen](#grenzen-und-offene-arbeiten).

### So bekommen Sie es

```bash
git clone https://github.com/c0decave/sandbox-ai && cd sandbox-ai
./install.sh --from .      # Befehle in ~/.local/bin; später: git pull && ./install.sh --from .
sandbox-ai doctor          # was auf diesem Host fehlt, mit dem Befehl, der es behebt
sandbox-ai build claude    # das Agent-Image (oder: codex, opencode, all)
sandbox-ai login claude    # außer Sie sind auf diesem Host bei Claude Code angemeldet
cd ~/src/myproject && sandbox-ai claude
```

Das [Schnellstart-Tutorial](QUICKSTART_DE.md) führt in fünfzehn Minuten hindurch; das [Lernvideo](docs/video/README.md#deutsch) zeigt die ganze Plattform in vierzehn Kapiteln (Deutsch und Englisch).

Ohne Klonen: [sandbox-ai.pyz, das AppImage oder die macOS-App](#vier-wege-zur-installation), fertig zum Starten.

## Inhalt

1. [Voraussetzungen](#voraussetzungen)
2. [Installation und Updates](#installation-und-updates)
3. [Der Befehl sandbox-ai](#der-befehl-sandbox-ai)
4. [Images bauen und aktualisieren](#images-bauen-und-aktualisieren)
5. [Agent starten](#agent-starten)
6. [Sicherheitsmodi](#sicherheitsmodi)
7. [Workspaces und Mounts](#workspaces-und-mounts)
8. [Anmeldung und Zustand](#anmeldung-und-zustand)
9. [Kits, Projekt-Memory und Plugins](#kits-projekt-memory-und-plugins)
10. [Netzwerkregeln](#netzwerkregeln)
11. [VPN](#vpn)
12. [LLM-Server: lokal und entfernt](#llm-server-lokal-und-entfernt)
13. [Lokale MCP-Server](#lokale-mcp-server)
14. [Andere Hosts](#andere-hosts)
15. [Projektkonfiguration und Wizard](#projektkonfiguration-und-wizard)
16. [Jeder Start: Namen, lösen, aufbewahren, fortsetzen](#jeder-start-namen-lösen-aufbewahren-fortsetzen)
17. [tmux-Modus](#tmux-modus)
18. [GUI-Programme und Browser](#gui-programme-und-browser)
19. [Hardware: Geräte und GPUs](#hardware-geräte-und-gpus)
20. [Agenten ohne Rückfragen](#agenten-ohne-rückfragen)
21. [Egress- und Dateizugriffs-Logs](#egress--und-dateizugriffs-logs)
22. [Aktivität der Agenten](#aktivität-der-agenten)
23. [Telemetrie der Agenten](#telemetrie-der-agenten)
24. [Datenschutzprüfung vor dem Veröffentlichen](#datenschutzprüfung-vor-dem-veröffentlichen)
25. [Verbindungs-Proxy](#verbindungs-proxy)
26. [Rückfragen und Browser-Anfragen](#rückfragen-und-browser-anfragen)
27. [Regelwerke](#regelwerke)
28. [Köderdateien (Canaries)](#köderdateien-canaries)
29. [Geheimnisse](#geheimnisse)
30. [Ressourcenlimits](#ressourcenlimits)
31. [Richtlinie des Administrators](#richtlinie-des-administrators)
32. [Weboberfläche](#weboberfläche)
33. [Desktop-App](#desktop-app)
34. [Optionsübersicht](#optionsübersicht)
35. [Tests](#tests)
36. [Fehlersuche](#fehlersuche)
37. [Grenzen und offene Arbeiten](#grenzen-und-offene-arbeiten)

## Voraussetzungen

Als normaler Host-Benutzer ohne `sudo` ausführen. Die Starter brauchen Bash,
Python 3.9 oder neuer und `readlink`.

- **Podman (Standard-Backend):** funktionierendes rootless Podman, delegierte
  UID/GID-Bereiche, `pasta` und ein vorgebautes Image je Agent.
- **Bubblewrap:** auf dem Host installierte Agenten, `bwrap`, `firejail`, `nft`,
  `setpriv`, `newuidmap`, `newgidmap` und für Netzwerk `slirp4netns`.
  `/etc/subuid` und `/etc/subgid` brauchen einen delegierten Bereich von
  mindestens 65536 IDs. Bubblewrap nutzt `/usr` des Hosts; Werkzeuge wie `tmux`,
  `Xvfb` oder `x11vnc` müssen für die jeweiligen Funktionen auf dem Host
  installiert sein. Bubblewrap behält Ihr Terminal und startet daher nicht
  interaktiv auf Kerneln, die Eingabe-Injektion per `TIOCSTI` erlauben
  (`dev.tty.legacy_tiocsti` ungleich 0 oder Kernel vor 6.2). Eine
  NVM-Installation braucht eventuell ein explizites,
  schreibgeschütztes `--runtime`-Verzeichnis.

Kernel und Hostpolicy müssen die nötigen Namespaces, Helfer und nftables
zulassen; die Starter heben Hostbeschränkungen nicht auf.

## Installation und Updates

### Vier Wege zur Installation

| Weg | Für | Braucht auf dem Host | Updates |
|---|---|---|---|
| `install.sh` | Befehlszeile und App, aktuell gehalten (empfohlen) | Bash, Python 3.9+, `readlink`; für die App PySide6 (holt der Installer) | `sandbox-ai-update`, signiert |
| `pip install sandbox-ai` | die Befehlszeile in einer Python-Umgebung | Python 3.9+ | `pip install -U sandbox-ai` |
| `sandbox-ai.pyz` | die Befehlszeile in einer Datei, nichts installiert | Python 3.9+ | neue Datei herunterladen |
| AppImage (Linux) / `sandbox-ai.app` (macOS) | die App mit der Befehlszeile darin, eigenem Python und Qt | Linux: glibc 2.34+ (aarch64: 2.39+) und die OpenGL/EGL-, xkbcommon- und fontconfig-Bibliotheken des Desktops; macOS 14+ | neue Datei herunterladen |

Jeder Weg startet die Sandboxes gleich und braucht dasselbe Backend: unter Linux rootless **Podman** oder
**Bubblewrap mit Firejail** (siehe [Voraussetzungen](#voraussetzungen)); unter macOS **Podman** mit einer
rootless `podman machine`, die `/Users` freigibt (`sandbox-ai doctor` prüft das und nennt die Befehle), und
Homebrews Bash für `sandbox-ai build` (`brew install bash`). Die Release-Dateien liegen auf der
[Release-Seite](https://github.com/c0decave/sandbox-ai/releases); `latest/download/NAME` nennt immer die neueste.

**sandbox-ai.pyz** - eine Datei, jedes Python ab 3.9:

```bash
mkdir -p ~/.local/bin && cd ~/.local/bin   # nicht in einen Projektordner: dort könnte ein Agent sie umschreiben
curl -fLO https://github.com/c0decave/sandbox-ai/releases/latest/download/sandbox-ai.pyz
chmod +x sandbox-ai.pyz
./sandbox-ai.pyz doctor                       # oder: python3 -I sandbox-ai.pyz doctor (jedes Python ab 3.9)
./sandbox-ai.pyz build claude
./sandbox-ai.pyz claude -w ~/src/myproject
```

sandbox-ai verweigert einen Workspace, einen rw-Mount oder `--state-dir`, der die `.pyz` (oder das AppImage) oder ihre ausgepackte Kopie enthält.

Beim ersten Start packt sie sich nach `~/.cache/sandbox-ai/pyz/VERSION-DIGEST-PYTHON/` aus (etwa eine Sekunde;
privat, schreibgeschützt, jede Datei gegen das Manifest in der `.pyz` geprüft) und läuft von dort; jeder spätere
Start prüft diese Kopie und verweigert sie, sobald sich darin etwas geändert hat (entfernen mit dem `chmod -R u+w
... && rm -rf ...` aus der Meldung, der nächste Start packt neu aus). Eine Kopie, der Dateien fehlen
(ein Cache-Aufräumer), wird neu ausgepackt. Die Prüfung findet Schäden, keine absichtliche Änderung mit den Rechten
Ihres eigenen Benutzers (der Nachweis liegt neben der Kopie); Sandboxes bekommen `~/.cache/sandbox-ai` nie (für den
Starter ein privater Ort), und ein Cache in einem Baum, den ein früherer Lauf einer Sandbox gegeben hat, wird
verweigert. Ein Start entfernt die Kopien anderer Versionen, aus denen nichts mehr läuft (unter Linux; unter
macOS selbst löschen: `chmod -R u+w ~/.cache/sandbox-ai/pyz && rm -rf ~/.cache/sandbox-ai/pyz`). `./sandbox-ai.pyz` startet
`python3 -I` (die Datei beginnt mit einer Shell-Zeile); ein anderes Python starten Sie selbst mit `-I`.

**AppImage (Linux, x86_64 und aarch64)** - die App, mit der Befehlszeile darin:

```bash
mkdir -p ~/.local/bin && cd ~/.local/bin   # nicht in einen Projektordner (siehe oben)
curl -fLO https://github.com/c0decave/sandbox-ai/releases/latest/download/sandbox-ai-x86_64.AppImage
chmod +x sandbox-ai-x86_64.AppImage
./sandbox-ai-x86_64.AppImage                  # die App
./sandbox-ai-x86_64.AppImage cli doctor       # die Befehlszeile: cli ARGUMENTE...
ln -s sandbox-ai-x86_64.AppImage ~/.local/bin/sandbox-ai   # als sandbox-ai aufgerufen ist es die Befehlszeile
```

(`sandbox-ai-aarch64.AppImage` auf ARM.) Ohne FUSE (Container, manche Minimalsysteme) so starten:
`TMPDIR=$(mktemp -d) ./sandbox-ai-x86_64.AppImage --appimage-extract-and-run ...`: Die Laufzeit packt nach
`$TMPDIR/appimage_extracted_<hash>` aus und verwendet einen Ordner dieses Namens wieder, also nie das gemeinsame
`/tmp` (AppRun verweigert einen Ordner, der einem anderen Benutzer gehört oder den andere schreiben können).
Der erste Start kopiert sein Python und sandbox-ai nach `~/.cache/sandbox-ai/appimage/`
(etwa 70 MB, geprüft wie bei der `.pyz`), damit Sessions und aufbewahrte Starts weiterlaufen, wenn das AppImage
beendet ist; PySide6 bleibt im AppImage. OpenGL/EGL kommt vom System, wie bei jedem Qt-Programm (Debian/Ubuntu:
`libegl1 libgl1 libopengl0 libxkbcommon0 libfontconfig1 libglib2.0-0 libdbus-1-3`, auf einem Desktop schon da); eine
X11-Sitzung braucht außerdem Qts xcb-Bibliotheken (`libxcb-cursor0 libxkbcommon-x11-0 libxcb-icccm4 libxcb-image0
libxcb-keysyms1 libxcb-render-util0 libxcb-xkb1`; Qt nennt eine fehlende, eine Wayland-Sitzung braucht keine davon).
Die Terminal-Tabs der App (QtWebEngine) sind nicht enthalten: Terminals öffnen sich in einem Terminalfenster. Ein
Start bei der Anmeldung, in der App eingerichtet, startet das AppImage.

**macOS (Apple Silicon und Intel, ab macOS 14)** - `sandbox-ai-arm64.dmg` oder `sandbox-ai-x86_64.dmg`: öffnen und
`sandbox-ai.app` nach Programme ziehen. Die App ist nicht mit einer Apple-Developer-ID signiert und nicht
notarisiert, deshalb verweigert macOS den ersten Start: unter macOS 14 Rechtsklick auf die App > **Öffnen** >
**Öffnen**; ab macOS 15 einmal öffnen, dann Systemeinstellungen > Datenschutz & Sicherheit > **Dennoch öffnen**.
Oder die Quarantäne-Markierung des Downloads selbst entfernen:

```bash
xattr -dr com.apple.quarantine /Applications/sandbox-ai.app
open /Applications/sandbox-ai.app
sudo ln -s /Applications/sandbox-ai.app/Contents/MacOS/sandbox-ai /usr/local/bin/sandbox-ai   # die Befehlszeile
sandbox-ai doctor                             # podman machine: läuft, rootless, /Users freigegeben
```

**Download prüfen.** `SHA256SUMS` listet jede Release-Datei, `SHA256SUMS.sig` signiert sie, sobald das Projekt einen
Release-Schlüssel hat ([Signierte Updates](#installation-und-updates), [RELEASING.md](RELEASING.md#check-a-release-by-hand)):

```bash
curl -fLO https://github.com/c0decave/sandbox-ai/releases/latest/download/SHA256SUMS
curl -fLO https://github.com/c0decave/sandbox-ai/releases/latest/download/SHA256SUMS.sig
printf 'release@sandbox-ai namespaces="sandbox-ai-release" %s\n' "ssh-ed25519 AAAA..." > signers  # der veröffentlichte Schlüssel
ssh-keygen -Y verify -f signers -I release@sandbox-ai -n sandbox-ai-release -s SHA256SUMS.sig < SHA256SUMS
sha256sum -c --ignore-missing SHA256SUMS      # macOS: shasum -a 256 -c --ignore-missing SHA256SUMS
```

Die Pakete aktualisieren sich nicht selbst: `sandbox-ai update` in einem davon sagt, wo es das neue gibt.

### install.sh

`install.sh` installiert die neueste Version oder aktualisiert darauf und hält
den Starter aus allen Workspaces heraus:

```bash
./install.sh                    # aus einem Checkout: neuestes Release der eingestellten Quelle
./install.sh --from .           # genau diesen Checkout
sandbox-ai-update               # später: auf das neueste Release aktualisieren (Installation aus einem Release)
sandbox-ai-update --check       # installierte und neueste Version, ihre Signatur und die vertrauten Schlüssel; ohne Änderung
sandbox-ai version --verbose    # wo es installiert ist und welchen Release-Schlüsseln es vertraut
./install.sh --uninstall        # Befehle und Installation entfernen (Geheimnisse, Logins, Kits, Sessions und Einstellungen bleiben)
```

| Modus | Dateien | Befehle |
|---|---|---|
| Benutzer (Standard) | `~/.local/share/sandbox-ai/versions/X.Y.Z`, Link `current` | Links in `~/.local/bin` und, falls vorhanden, `~/.bin` |
| System (`--system` oder als root) | `/usr/local/lib/sandbox-ai` (`--prefix`) | `/usr/local/bin` |

Eine Benutzerinstallation legt das Befehlsverzeichnis auf Ihren `PATH`:
`~/.bashrc` (und `~/.zshrc`, falls vorhanden) bekommt einen Block zwischen
`# >>> sandbox-ai >>>` und `# <<< sandbox-ai <<<`. Der Block fügt das
Verzeichnis nur hinzu, wenn es noch nicht im `PATH` ist. Erneutes Installieren
ändert nichts, ein anderes `--bin-dir` ersetzt den Block, und `--uninstall`
entfernt ihn. `--no-modify-rc` lässt die Dateien in Ruhe und gibt stattdessen
die Zeile aus, die Sie selbst eintragen. Ein neues Terminal (oder
`source ~/.bashrc`) findet die Befehle.

Releases kommen von `https://github.com/c0decave/sandbox-ai`, sofern
`--source URL` oder `SANDBOX_AI_SOURCE` kein anderes Gitea-, Forgejo- oder
GitHub-Repository nennt. Der Installer lädt `sandbox-ai-bundle-X.Y.Z.tar.gz`,
`SHA256SUMS` und `SHA256SUMS.sig`, prüft die Signatur, bricht bei falscher
Prüfsumme ab, entpackt nur reguläre Dateien unterhalb eines Verzeichnisses und
schaltet den Link `current` atomar um; die vorige Version bleibt für ein
Zurückrollen erhalten. Für ein privates Repository einen Token in
`SANDBOX_AI_TOKEN` legen: Er geht nur an den Host des Repositorys, nie per
unverschlüsseltem http außer an localhost. Bei GitHub hebt ein Token auch die
API-Grenze von 60 Anfragen pro Stunde und Adresse auf (der Installer sagt, wenn
sie erreicht ist). Vorhandene Dateien in den bin-Verzeichnissen, die nicht zu
sandbox-ai gehören, bleiben unberührt (`--force` ersetzt sie).

**Signierte Updates.** Ein Release signiert seine `SHA256SUMS` mit einem
SSH-Schlüssel (`ssh-keygen -Y sign`, `SHA256SUMS.sig`); `sandbox/release-signers`
im installierten Baum nennt die Schlüssel, denen er vertraut. Sobald die Datei
einen Schlüssel enthält, wird ein Update nur mit einer gültigen Signatur durch
einen davon installiert, geprüft mit `ssh-keygen` (OpenSSH 8.1 oder neuer),
bevor irgendetwas entpackt wird: Ein unsigniertes Release, eine Signatur durch
einen anderen Schlüssel oder für einen anderen Zweck und eine veränderte
`SHA256SUMS` werden abgelehnt. Die Schlüssel ändern (rotieren) kann nur ein
Release, das ein jetzt vertrauter Schlüssel signiert hat. Eine Erstinstallation
vertraut den Schlüsseln, die mit `install.sh` kamen (das veröffentlichte
`install.sh` trägt sie; Vertrauen beim ersten Gebrauch); vergleiche daher den
Fingerabdruck aus `sandbox-ai version --verbose` mit dem, den das Projekt
veröffentlicht. Releases, die älter als die installierte Version sind, werden
abgelehnt, und ein „neuestes“ Release, das älter ist als ein schon gesehenes,
wird gemeldet.

| Option | Wirkung |
|---|---|
| `--allow-downgrade` | ein Release installieren, das älter als das installierte ist |
| `--allow-unsigned` | ein Release ohne gültige Signatur installieren, mit deutlicher Warnung; die vertrauten Schlüssel ändern kann es trotzdem nicht |
| `--from BUNDLE.tar.gz --verify` | ein heruntergeladenes Bundle gegen die `SHA256SUMS` und `SHA256SUMS.sig` daneben prüfen |

Ein Administrator kann Signaturen mit `"require_signed_updates": true` in
`/etc/sandbox-ai/policy.json` verlangen ([Richtlinie](#richtlinie-des-administrators)):
`--allow-unsigned` und `--from VERZEICHNIS` werden abgelehnt, `--from BUNDLE`
wird immer geprüft, und eine Installation, die keinem Schlüssel vertraut, kann
nicht aktualisieren. Schlüssel, Rotation, Widerruf und die Grenzen stehen in
[RELEASING.md](RELEASING.md#signed-releases).

`pip install sandbox-ai` (oder `pipx install sandbox-ai`) installiert dieselben
Befehle von PyPI, sobald Releases dort erscheinen. Ein Checkout funktioniert
auch ohne Installation (`./sandbox-ai claude`), darf aber selbst kein Workspace
sein: Der Agent könnte den Starter umschreiben. Wie Releases gebaut und
veröffentlicht werden, steht in [RELEASING.md](RELEASING.md).

## Der Befehl sandbox-ai

`sandbox-ai` bündelt alle Werkzeuge. Die älteren Einzelbefehle
(`claude-sandbox`, `codex-sandbox`, `opencode-sandbox`, `sandbox-session`,
`sandbox-mcp`, `sandbox-wizard`, `sandbox-build-image`) funktionieren noch,
sind aber **veraltet**: Jeder gibt einen Hinweis auf stderr aus, der seine
`sandbox-ai`-Form nennt (`SANDBOX_AI_NO_DEPRECATION=1` blendet ihn aus, für
Skripte), und sie werden in einer späteren Version entfernt.


**Tab-Vervollständigung** für bash, zsh und fish ergänzt jeden Befehl,
Unterbefehl und jede Option, die Werte von Optionen (`--mode`, `--telemetry`,
`--backend` …) und die Namen, die Sie haben: Starts (`attach`, `stop`, `resume`),
laufende Sandboxes (`net`, `devices`, `canaries`), MCP-Server, LLM-Server,
VPN-Profile und Kits; Pfade ergänzt die Shell. `install.sh` richtet sie für
alle drei ein (die Skripte in `~/.local/share/bash-completion/completions/`,
`~/.local/share/zsh/site-functions/` und `~/.config/fish/completions/`, dazu die
Zeilen, die bash und zsh im Block der `~/.bashrc`/`~/.zshrc` brauchen). Von
Hand: `sandbox-ai completion install`, oder `sandbox-ai completion bash|zsh|fish`
für das Skript selbst. Die Vorschläge kommen aus den Parsern der Befehle selbst
und passen so immer zur installierten Version.

| Befehl | Ältere Form | Macht |
|---|---|---|
| `sandbox-ai claude\|codex\|opencode [Optionen] [-- Argumente]` | `claude-sandbox` … (veraltet) | einen Agenten starten |
| `sandbox-ai list [--all] [--json]` | `sandbox-ai ps` | eine Tabelle aller Starts: was läuft oder pausiert, die Starts mit aufbewahrtem HOME und die MCP-Server-Sandboxen ([jeder Start](#jeder-start-namen-lösen-aufbewahren-fortsetzen)) |
| `sandbox-ai list --history NAME [--json]`, `sandbox-ai rename ALT NEU` | | was mit einem Start geschah (Start, Ende mit Status, Pause, Fortsetzen, Stopp, Wiedergeburt, Umbenennung); einem Start einen anderen Namen geben (seine Run-ID bleibt) |
| `sandbox-ai attach\|pause\|resume\|stop\|delete NAME` | `sandbox-ai session …` | jeden Start per Name oder Run-ID: dieses Terminal verbinden (lösen mit Ctrl-b d oder durch Schließen), einfrieren und auftauen, beenden, einen gestoppten samt HOME entfernen |
| `sandbox-ai resume NAME [--restart] --workspace DIR` / `--mount QUELLE:ZIEL[:ro\|rw]` | | einen aufbewahrten Start (neu) mit einem weiteren Workspace oder Mount starten; die Pfade werden wie bei einem neuen Start geprüft, der Agent setzt sein Gespräch fort, und die Ergänzung bleibt für spätere Starts. `--restart` beendet eine laufende vorher; `sandbox-ai workspace add` fügt einen Workspace ohne Neustart hinzu |
| `sandbox-ai resume NAME --review-home [--json]` | | was sich in den Startdateien eines aufbewahrten HOME (Shell, Git, SSH, Einstellungen, Hooks, Plugins des Agenten) seit seinem letzten Start geändert hat; startet nichts ([jeder Start](#jeder-start-namen-lösen-aufbewahren-fortsetzen)) |
| `sandbox-ai gc [--older-than TAGE] [--dry-run] [--json]` | | Reste entfernen; mit TAGE auch die gestoppten aufbewahrten Starts, die so lange unbenutzt sind, samt HOME |
| `sandbox-ai session …` | `sandbox-session` (veraltet) | dasselbe für Sessions, in der älteren Form |
| `sandbox-ai mcp …` | `sandbox-mcp` (veraltet) | MCP-Server-Sandboxen |
| `sandbox-ai wizard [new\|edit\|trust\|forget\|models] …` | `sandbox-wizard` (veraltet) | Projekt einrichten, bearbeiten und vertrauen |
| `sandbox-ai login claude\|codex\|opencode` | | in einer Sandbox anmelden; das Login gilt für alle Sandboxes dieses Agenten ([Login-Speicher](#login-speicher-für-sandboxes)) |
| `sandbox-ai login --status`, `sandbox-ai login AGENT --logout` | | welches Login die Sandboxes jedes Agenten bekommen; gespeichertes Login vergessen |
| `sandbox-ai plugins claude [--update] [--marketplaces-only]`, `sandbox-ai plugins --status`, `sandbox-ai plugins marketplaces [MARKTPLATZ] [--agent A] [--json]`, `sandbox-ai plugins claude -- ARGS`, `sandbox-ai plugins claude trust-hooks PLUGIN` | | die Claude-Plugins der Kits in den Plugin-Store installieren ([Plugins](#kits-projekt-memory-und-plugins)); deklariert vs. installiert zeigen; jeden Marktplatz mit seinen Plugins und ihrem Zustand ([Marktplätze](#die-plugin-marktplätze-sandbox-ai-plugins-marketplaces)); `claude plugin ARGS` auf dem Store ausführen; die Hooks eines Plugins freigeben ([Hooks in Kits](#hooks-in-kits)) |
| `sandbox-ai kits [list\|show\|new\|add-skill\|new-skill\|add-plugin\|enable-plugin\|disable-plugin\|add-marketplace\|remove-marketplace\|import-marketplace\|remove-skill\|remove-plugin\|add-hook\|remove-hook\|trust-hooks\|delete] …` | | alle Kits und was sie mitbringen (`--json` für Programme); eigene Kits anlegen, Skills kopieren oder schreiben, Plugins und Marktplätze deklarieren, ein- und ausschalten, den Marktplatz eines Agents importieren, Hooks hinzufügen und freigeben ([Kits](#kits-verwalten-sandbox-ai-kits), [Hooks in Kits](#hooks-in-kits)) |
| `sandbox-ai build [--pin V] [--extension-only] AGENT\|all\|VARIANTE`, `sandbox-ai build team\|vpn` | `sandbox-build-image` (veraltet) | Images und ihre Erweiterungen bauen ([Erweiterungen](#erweiterungen-und-varianten)), das Team-Image für `--with` ([Andere Agenten in derselben Sandbox](#andere-agenten-in-derselben-sandbox)) oder den VPN-Container ([VPN](#vpn)) |
| `sandbox-ai doctor [--backend podman\|bwrap\|both] [--plain] [--no-color] [--json]` | | eine Tabelle nach Bereichen (System, Engine, Agenten, Projekt), im Terminal farbig (ok grün, WARN orange, FAIL rot; ohne mit `--no-color` oder `NO_COLOR`; `--plain` eine Zeile je Prüfung); Host prüfen: Benutzer, Python, User-Namespaces, Podman (rootless), pasta, subuid/subgid, Images mit Agent-Versionen, Bubblewrap-Werkzeuge, TIOCSTI, Host-Logins (nur Ablauf), Standard-Kit und Kits mit ihren Skills, Agents, Commands und Prompts, Claude-Plugins deklariert vs. installiert, das Memory des Projekts im aktuellen Verzeichnis, Git-Identität, Projekt-Vertrauen; zu jedem Problem der Befehl, der es behebt; Exit 1 bei einem Fehler |
| `sandbox-ai logs [VERZ\|--project P] [--json]` | | blockierte Verbindungen und Dateizugriffe eines `--log-dir` zusammenfassen und je blockiertem Ziel die passende Option vorschlagen (`--allow-pypi`, `--allow-host`, `--allow-tcp`, `--allow-host-port` …) |
| `sandbox-ai activity [LAUF\|--session NAME\|--project P\|--log-dir VERZ] [-f] [--since Z] [-k ART] [--json]` | | was die Agenten getan haben: Eingaben, Antworten, Tool-Aufrufe, Befehle, Dateiänderungen, Netzwerk, Rückfragen, Tokens ([Aktivität der Agenten](#aktivität-der-agenten)) |
| `sandbox-ai net list\|show\|allow\|revoke\|untaint …` | | ändern, was eine laufende Sandbox erreichen darf ([Netzwerkänderungen](#netzwerk-einer-laufenden-sandbox-ändern)); beendet wird sie mit `sandbox-ai stop NAME` |
| `sandbox-ai devices list\|add\|remove [LAUF] PFAD\|usb:VID:PID`, `sandbox-ai limits [LAUF] …` | | Geräte und Limits einer laufenden Sandbox ([Hardware](#hardware-geräte-und-gpus), [Limits](#ressourcenlimits)) |
| `sandbox-ai workspace list\|add\|remove [LAUF] VERZ\|DATEI [--ro] [--name NAME] [--force]` | | ein Host-Verzeichnis (oder eine einzelne Datei), einer laufenden Sandbox unter `/workspaces/<name>` (oder `/workspaces/NAME`) hinzugefügt, mit den Prüfungen eines Starts, und wieder weggenommen |
| `sandbox-ai approvals list\|allow\|deny\|history\|remembered\|forget …` | | beantworten, was laufende Sandboxen fragen; was immer erlaubt wurde ([Rückfragen](#rückfragen-und-browser-anfragen)) |
| `sandbox-ai canaries list [LAUF]`, `sandbox-ai canaries resume LAUF` | | die Köderdateien der Läufe und was mit ihnen geschah; eine Sandbox fortsetzen, die ein Köder angehalten hat ([Köderdateien](#köderdateien-canaries)) |
| `sandbox-ai secrets list\|set\|delete\|backend` | | API-Schlüssel und Tokens im System-Schlüsselbund ([Geheimnisse](#geheimnisse)) |
| `sandbox-ai git-logins add\|list\|remove\|check …` | | die Logins Ihrer Git-Server, vom Verbindungs-Proxy eingesetzt, nie in einer Sandbox ([Git-Logins](#git-logins-im-proxy-sandbox-ai-git-logins)) |
| `sandbox-ai publish-logins add\|list\|remove\|check\|kinds …` | | Ihre Logins für npm, PyPI, crates.io, GitLab und GitHub, für `--publish` ([Veröffentlichen](#veröffentlichen-in-registries-und-forges-sandbox-ai-publish-logins---publish)) |
| `sandbox-ai identities add\|set\|login\|logout\|publish\|unpublish\|list\|show\|check\|remove …` | | Commit-Name und -Adresse, Git- und Publish-Logins unter einem Namen, für `--identity` ([Identitäten](#identitäten-sandbox-ai-identities---identity)) |
| `sandbox-ai presets list\|show\|check …`, `sandbox-ai demo-device start\|serve\|status\|stop` | | fertige Einrichtungen für `--preset`; ein nachgebildetes serielles Gerät zum Üben ([Presets](#presets)) |
| `sandbox-ai team start\|list\|show\|post\|stop\|delete …` | | ein Team von Agenten, je eine Sandbox, mit einem Aufgabenbrett ([Ein Team von Agenten](#ein-team-von-agenten)) |
| `sandbox-ai hosts list\|add\|set\|remove\|test\|update\|run …` | | andere Rechner (oder LXC-Container), deren sandbox-ai dieses über SSH verwaltet und aktualisiert ([Andere Hosts](#andere-hosts)) |
| `sandbox-ai web user\|vendor\|serve\|cert …`, `sandbox-ai defaults show\|set\|unset …` | | die Weboberfläche; Ihre Vorgaben für jeden Start in `~/.config/sandbox-ai/defaults.json` ([Weboberfläche](#weboberfläche)) |
| `sandbox-ai completion bash\|zsh\|fish\|install` | | Tab-Vervollständigung (siehe oben) |
| `sandbox-ai llm add\|list\|show\|remove\|models\|check …` | | LLM-Server, lokal oder entfernt, für `--llm NAME[:MODELL]`; ihre Schlüssel bleiben beim Verbindungs-Proxy ([LLM-Server](#llm-server-lokal-und-entfernt)) |
| `sandbox-ai vpn add\|list\|show\|remove\|check …` | | VPN-Profile für `--vpn` (WireGuard, OpenVPN, Mullvad, Tailscale, Tor); ihre Schlüssel bleiben in `sandbox-ai secrets` ([VPN](#vpn)) |
| `sandbox-ai images [packages\|extend\|check\|recipe\|new\|remove\|history] [--json]` | | die Agent-Images und Varianten mit Agent-Version, Alter, Größe und ob sie zu ihrer Erweiterung passen; die Pakete eines Images; Erweiterungen, fertige Rezepte (`media`, `tor`) und Varianten ([Erweiterungen](#erweiterungen-und-varianten)) |
| `sandbox-ai update [--check] [--version X.Y.Z] [--allow-downgrade]` | `sandbox-ai-update` | eine mit `install.sh` gemachte Installation aktualisieren; signierte Releases ([Installation](#installation-und-updates)) |
| `sandbox-ai version [--verbose]` | `-V`, `--version` | Version; `--verbose`: wo es installiert ist und die Fingerabdrücke der vertrauten Release-Schlüssel |

## Images bauen und aktualisieren

```bash
./sandbox-ai build all                 # neueste Versionen, alle drei Images
./sandbox-ai build claude              # ein Image, neuestes Claude Code
./sandbox-ai build --pin 1.0.42 claude # genau diese Agent-Version
```

Jeder Agent hat ein eigenes `sandbox/Containerfile.<agent>` und das Image
`localhost/<agent>-sandbox:latest`; ein Image enthält nur die eigene CLI. Die
Starter bauen oder laden nie automatisch Images. `./sandbox-ai build vpn`
baut `localhost/sandbox-ai-vpn:latest` aus `sandbox/Containerfile.vpn`, den
Container von [`--vpn`](#vpn) (Debian mit WireGuard-Werkzeugen, OpenVPN und
nftables, wireguard-go aus den Quellen gebaut, die Programme von Tailscale aus
dessen offiziellem Image).

**Basis und Updates.** Basis ist `node:lts-bookworm-slim`: die aktuelle
Active-LTS-Linie von Node.js mit dem neuesten Patch-Release auf Debian 12.
Jeder Build nutzt `--pull=newer`; LTS-Bug- und Sicherheitsfixes kommen so mit
jedem Neubau, und bei einer neuen LTS-Linie wandert der Tag mit. Eine Linie
fixieren: `--build-arg NODE_IMAGE=docker.io/library/node:24-bookworm-slim`.

**Standard: neueste Version, fixieren auf Wunsch.** Jeder Build fragt zuerst
für jeden gewählten Agenten die neueste Version bei der npm-Registry ab (per
`npm view`, ohne npm per Python) und übergibt sie als `<AGENT>_VERSION`; der
zwischengespeicherte `npm install`-Layer wird so bei jeder neuen Version neu
gebaut. Schlägt eine Abfrage fehl, wird nichts gebaut. Zum Fixieren:

| Option | Fixiert |
|---|---|
| `--pin VERSION` (oder `--build-arg CLAUDE_VERSION=…`) | den Agenten (ein Agent pro Build) |
| `--uv VERSION` | den Tag des uv-Images (Standard `latest`) |
| `--node IMAGE` | das Node.js-Basis-Image, als Tag oder `@sha256:`-Digest |

Jedes Image trägt das Label `sandbox-ai.agent-version`, das
`sandbox-ai doctor` anzeigt. Für eine entfernte Engine `--podman-url URL`
angeben.

**Inhalt der Images.** Bash, Git, die GitHub-CLI `gh`, Vim, Python 3 mit pip
und venv, uv/uvx, ripgrep, curl, wget, `nc` (netcat-traditional),
`host`/`dig`, `ping`, `traceroute`, `ss`/`ip` (iproute2), `ssh` (openssh-client), jq, openssl, tmux, Firejail und
nftables; für `--gui` zusätzlich Xvfb, x11vnc, x11-utils, Schriften
und die Laufzeitbibliotheken der Playwright-Builds von Chromium und Firefox.
Keine Compiler-Toolchain; Projektwerkzeuge mit einer [Erweiterung](#erweiterungen-und-varianten) ergänzen. `gh` kommt aus
GitHubs eigenem apt-Repository (`cli.github.com`, neueste Version zum
Build-Zeitpunkt); in der Sandbox braucht es `--allow-github` und ein Token,
z. B. `--git-token-env https://github.com=NAME`, das auch `GH_TOKEN` setzt.

Netzwerkwerkzeuge unterliegen der Firewall: `dig`/`host` brauchen `--dns`,
`ping` braucht ICMP und `traceroute` seine UDP-Proben (`--allow IP` oder
`--egress-all-allow`); `ss` zeigt nur die Sockets der Sandbox. Das
Root-Dateisystem ist schreibgeschützt und das System-Python ist als extern
verwaltet markiert; Pakete daher in eine virtuelle Umgebung installieren
(`python3 -m venv .venv`, `uv venv`) oder `uvx`/`uv tool` nutzen. Paketindizes
sind standardmäßig gesperrt; freigeben mit
`--allow-host pypi.org --allow-host files.pythonhosted.org`.

### Erweiterungen und Varianten

Die Images bleiben klein; was ein Projekt braucht, kommt als **Erweiterung**
dazu, statt die Rezepte zu ändern. Eine Erweiterung gehört zum Image eines
Agenten oder zu einer benannten **Variante** davon und enthält Debian-Pakete,
globale npm-Pakete, Python-Werkzeuge (mit `uv tool` installiert), Dateien zum
Hineinkopieren und optional ein rohes Containerfile-Fragment:

```bash
sandbox-ai images extend claude --add-apt build-essential --add-pip ruff==0.6.9
sandbox-ai images extend claude --add-file ./lint.sh:/usr/local/bin/lint:0755
sandbox-ai images extend claude --fragment mein.fragment   # oder - für die Standardeingabe
sandbox-ai images check claude      # Befunde und das fertige Containerfile
sandbox-ai build claude             # Basis-Image, dann die Erweiterungsschicht

sandbox-ai images new gpu --from claude    # eine Variante: localhost/claude-sandbox-gpu
sandbox-ai images extend gpu --add-apt mesa-vulkan-drivers
sandbox-ai build gpu                       # die Basis von claude, dann diese Variante
sandbox-ai claude --image-variant gpu      # oder "-iv gpu" in den options einer Projektdatei
```

| Befehl | Tut |
|---|---|
| `images extend AGENT\|VARIANTE` | `--add-apt/--remove-apt PAKET`, `--add-npm/--remove-npm PAKET`, `--add-pip/--remove-pip PAKET`, `--add-file QUELLE:ZIEL[:MODUS]`, `--remove-file ZIEL`, `--fragment DATEI\|-`, `--clear-fragment`; ohne Änderung (oder mit `--show`) zeigt es die Erweiterung; `--json` |
| `images check AGENT\|VARIANTE [--fragment DATEI\|-]` | prüfen (auch ein ungespeichertes Fragment) und das fertige Containerfile ausgeben; Exit 1 bei Fehlern |
| `images new NAME --from AGENT [--empty]` | eine Variante, ausgehend von der Erweiterung des Agenten, außer mit `--empty` |
| `images remove AGENT\|VARIANTE [--keep-image]` | eine Erweiterung entfernen; das Image einer Variante mit |
| `images history [AGENT\|VARIANTE] [--json]` | jede Änderung und jeder Build, mit dem Hash der Erweiterung |
| `build [-e\|--extension-only] AGENT\|all\|VARIANTE` | `--extension-only` baut nur die Schichten neu, auf dem vorhandenen Basis-Image |

**Wo sie liegen.** `~/.config/sandbox-ai/images/NAME/` (`image.json`, das
optionale `Containerfile.fragment` und `files/`), nur für Sie lesbar
(0700/0600). Jeder Befehl lehnt diesen Ordner - und jede Quelle von
`--add-file` - ab, wenn er in einem Baum liegt, den eine Sandbox beschreiben
kann oder konnte (sandbox-ais Verzeichnis freigegebener Workspaces, rw-Mounts
und HOMEs), oder über einen dort angelegten Symlink erreicht wird. Dateien
werden beim Hinzufügen kopiert (nur reguläre Dateien, nie Links, je 50 MB);
ein Build nimmt genau die Bytes, die er gehasht hat, aus einem frischen
privaten Build-Kontext, der nichts anderes enthält.

**Wie gebaut wird.** `sandbox-ai build AGENT` baut das Basis-Image wie bisher
und taggt es `localhost/AGENT-sandbox:base`. Ohne Erweiterung ist es auch
`:latest`; mit einer wird eine Schicht `FROM localhost/AGENT-sandbox:base`
(`--pull=never`) zu `:latest`, und die Varianten des Agenten entstehen auf
derselben Basis als `localhost/AGENT-sandbox-NAME:latest`. Jede Schicht trägt
die Labels `sandbox-ai.extension-hash` (erzeugtes Containerfile und Dateien),
`sandbox-ai.base-id` und, geerbt, `sandbox-ai.agent-version`; der Build-Cache
verwendet unveränderte Schichten wieder. `sandbox-ai images` zeigt je Image,
ob es **aktuell** ist (`current`), **veraltet** (`outdated`: die Erweiterung
hat sich geändert), auf einer **neueren Basis** fußen sollte (`base-changed`)
oder **ungültig** ist (`invalid`). Das Containerfile endet immer mit
`USER root` und `WORKDIR /work`, wie der Launcher es erwartet.
`--image-variant NAME` prüft, dass NAME eine Variante des Agenten ist, gilt
nur für Podman und schließt `--image` aus; ein `forbid: ["--image"]` des
Administrators lehnt jedes Image außer dem Standard ab, Varianten
eingeschlossen.

**Die Prüfung** (`images check`, und bei jeder Änderung und jedem Build):
Fehler verhindern Speichern und Bauen, Warnungen werden angezeigt.

| Regel | Stufe | Was |
|---|---|---|
| `from` | Fehler | `FROM` - die Basis ist immer das Image des Agenten |
| `entrypoint` | Fehler | `ENTRYPOINT`, `CMD` |
| `volume`, `onbuild` | Fehler | `VOLUME` (ein beschreibbares Host-Volume am schreibgeschützten Root vorbei), `ONBUILD` |
| `setuid`, `setcap` | Fehler | `chmod u+s`/`4755`, `install -m 4755`, `COPY --chmod=4755`, ein Dateimodus mit setuid/setgid; `setcap` |
| `protected` | Fehler | Schreiben, Überlinken oder Entfernen dessen, was die Einrichtung der Sandbox als root ausführt: `python3`, `nft`, `firejail`, `setpriv`, `tmux`, `sh`, `bash` (auch verdeckt in `/usr/local/bin`), `/etc/ld.so.preload`, `/sandbox`, das CA-Bündel |
| `remove-protected` | Fehler | `apt-get remove/purge` von firejail, nftables, python3, util-linux, tmux, bash, ca-certificates, ... |
| `add-url`, `add-git` | Fehler | `ADD` von einer URL ohne `--checksum=sha256:…`; `ADD` eines Git-Repositorys |
| `copy-from`, `copy-source` | Fehler | `COPY --from`; eine Quelle, die nicht `files/NAME` dieser Erweiterung ist |
| `loader` | Fehler | `ENV`/`ARG` `LD_PRELOAD`, `LD_AUDIT`, `LD_LIBRARY_PATH` |
| `label` | Fehler | Labels `sandbox-ai.*` |
| `heredoc`, `control`, `syntax`, `unknown`, `size` | Fehler | Here-Dokumente, Steuer- oder Bidi-Zeichen, unlesbare Zeilen, unbekannte Anweisungen, über 64 KiB |
| `package`, `agent-package`, `file`, `mode` | Fehler | ungültige Paketnamen (keine URLs, Pfade oder Git-Quellen), das npm-Paket des Agenten selbst, fehlerhafte Dateieinträge, für alle beschreibbare Modi |
| `pipe-shell`, `ca-trust` | Warnung | `curl … \| sh`; neue vertrauenswürdige CAs |
| `user`, `workdir`, `shell`, `env` | Warnung | `USER`, `WORKDIR`, `SHELL`; `ENV PATH`, `HOME`, Proxy- und CA-Variablen, die der Launcher setzt |
| `masked`, `add-local`, `no-effect`, `unknown-flag` | Warnung | Dateien nach `/work`, `/root`, `/tmp` … (beim Start überdeckt); `ADD` einer lokalen Datei; `EXPOSE`, `HEALTHCHECK` …; unbekannte Optionen |

Die Prüfung schützt vor Versehen und kopierten Schnipseln, sie ist kein
Beweis: eine `RUN`-Zeile kann weiterhin alles, was ein Build kann. Was Sie in
ein Image legen, läuft in jeder Sandbox, die daraus startet.

## Agent starten

```bash
./sandbox-ai claude --workspace "$HOME/src/demo"
./sandbox-ai codex --workspace "$HOME/src/demo" \
  -- exec --skip-git-repo-check 'Lege hello.txt mit dem Inhalt Hallo an.'
./sandbox-ai opencode --workspace "$HOME/src/demo" --dry-run   # nur den Plan zeigen
./sandbox-ai codex --workspace "$HOME/src/demo" --offline --probe   # Isolation prüfen
```

Argumente nach `--` gehen unverändert an den Agenten. `--dry-run` zeigt den
vollständigen Plan (Workspaces, Netz, Auth-Kopien, GUI, Session), ohne
Anmeldedaten zu lesen, Namen aufzulösen oder etwas anzulegen.

**Claude** nutzt standardmäßig `claude-opus-5-5` mit Effort `high`: Der Starter
schreibt diese Werte in `~/.claude/settings.json` des Sandbox-HOME, sofern sie
dort noch nicht gesetzt sind; Einstellungen in einem `--state-dir` haben also
Vorrang, und `-- --model sonnet --effort medium` überschreibt sie je Aufruf.
Liegt in der Sandbox ein gültiger Claude-Login, setzt der Starter außerdem
`hasCompletedOnboarding` und ein `theme` in der `~/.claude.json` der Sandbox,
falls sie fehlen; sonst verlangt Claudes Ersteinrichtung trotz gültiger
Anmeldedaten einen neuen Login.
Von der `~/.claude.json` des Hosts wird nur der Account-Teil (`oauthAccount`,
`userID`, Onboarding-Status, Theme) kopiert; MCP-Server-Definitionen, die oft
API-Schlüssel enthalten, und die Liste der Host-Projekte bleiben auf dem Host.
In der Sandbox erneuerte Tokens gehen noch während des Laufs an ihre Quelle
zurück, und ein neueres Login der Quelle erreicht die laufende Sandbox auf
demselben Weg (siehe **Claude-Anmeldung** unten); beim nächsten Start ersetzt
immer das Login der Quelle das der Sandbox.

**Codex** erhält standardmäßig `--sandbox danger-full-access`: Es verlässt sich
auf die geprüfte äußere Sandbox und Firejail, weil seine eigene verschachtelte
Sandbox darin kein UID-Mapping anlegen kann. Explizite Optionen `--sandbox`,
`-s` und `--dangerously-bypass-approvals-and-sandbox` bleiben erhalten.

### Presets

Ein Preset ist ein fertiges Setup zum Starten: Optionen des Launchers,
MCP-Server, ein Regelwerk und eine erste Aufgabe für den Agenten, mit
Platzhaltern für das, was nur Sie wissen (ein Ordner, ein Port, ein Gerät). Die
Kommandozeile gewinnt gegen das Preset; Projektdatei, Voreinstellungen,
Sicherheitsmodus und die Richtlinie des Administrators gelten wie immer. Der
`--mode` eines Presets ist eine Untergrenze: Er hebt einen laxeren Modus an,
senkt aber nie einen strengeren aus Projektdatei oder Voreinstellungen (ein
selbst getipptes `--mode` gewinnt).

```bash
sandbox-ai presets list                                  # eingebaute und eigene
sandbox-ai presets show software-analysis                # was es setzt und fragt
sandbox-ai presets check software-analysis --agent claude --value samples=~/samples
sandbox-ai claude --preset software-analysis --preset-value samples=~/samples
```

Eingebaut: **Analyse** (`hardware-analysis`, `software-analysis`,
`network-analysis`, `forensics`, `firmware-iot`), **Entwicklung**
(`development`, `code-audit`, `docs-writer`, `data-ml`, `mobile-android`,
`onboarding-buddy`), **lokale Modelle** (`local-api` für jeden
OpenAI-kompatiblen Server, `ollama`, `lm-studio`, `llama-cpp`, `vllm`: `--llm
NAME[:MODELL] --no-provider-defaults`; Claude Code nur mit einem Server, der die
Anthropic-Messages-API hat), **Privatsphäre** (`tor`: alles durch ein
Tor-Profil von `sandbox-ai vpn add NAME --tor`, mit `--dns` für `.onion`-Namen),
**Tests** (`commit-censor`, `chaos-monkey`,
`docs-lie-detector`, `canary-course`, `network-maze`, `memory-hog`) und **Spaß**
(`time-capsule`, `mini-ctf`, `archivist-1995`, `radio-sandbox-ai`,
`agent-race`, `pixel-art-factory`, `sand-chess`). Die Analyse-Presets lassen
den Agenten nur zu seinem Provider und die Proben schreibgeschützt; ihre
Werkzeuge (capa, FLOSS, radare2, YARA, binwalk, TShark, The Sleuth Kit ...) sind
MCP-Server aus den Katalogen, jeder in einem eigenen Container: `presets check`
nennt die `sandbox-ai mcp add`-Befehle für die noch nicht hinzugefügten und
die VPN-Profile und LLM-Server in den Werten eines Presets, die nicht
eingerichtet sind (oder die der Agent nicht nutzen kann).

Eigene Presets sind JSON-Dateien in `~/.config/sandbox-ai/presets/` (außerhalb
jedes Workspaces: Ein Agent darf nicht schreiben, womit er startet); eines mit
der ID eines eingebauten ersetzt dieses. Ein Preset enthält nie ein Geheimnis
(Optionen nennen sie nur) und darf keinen Login hineinkopieren (`--copy-auth`,
`--auth-file`, ...), den Workspace wählen oder `--rules` tragen (eine
`"rules"`-Datei daneben wird dafür übergeben). Das Format steht in
`sandbox-ai presets --help` und `sandbox/presets.py`.

Weitere Preset-Ordner, eine private Sammlung oder der Checkout eines Teams,
kommen mit `sandbox-ai presets dirs add ORDNER` hinzu (aufgelistet mit
`presets dirs`, entfernt mit `presets dirs remove ORDNER`; gespeichert als
`"preset_dirs"` in `~/.config/sandbox-ai/defaults.json`, in der App auch unter
Einstellungen > sandbox-ai). Ihr eigener Ordner gewinnt vor ihnen, der zuerst
genannte vor den späteren, und sie vor den eingebauten Presets. Aus demselben
Grund wie oben wird ein Ordner in einem Workspace oder einem anderen Ordner, in
den Sandboxen schreiben können oder konnten, abgelehnt (und übergangen, wenn er
es später wird), und einem Preset oder Regelwerk dort, das ein Symlink ist, wird
nicht gefolgt.

**Ein Gerät zum Üben.** `sandbox-ai demo-device start` startet ein emuliertes
serielles Gerät auf `127.0.0.1:4000` (den erfundenen „SB-42 Sensorknoten“:
U-Boot, Bootmeldungen eines Kernels, ein Login und eine kleine Shell mit
lesbarem Flash), so wie ser2net ein echtes anbietet; `hardware-analysis` nutzt
es standardmäßig (`--allow-host-port 4000`; pyserial spricht es als
`socket://127.0.0.1:4000` an). Es ist fester Text: Auf Ihrem Rechner wird nichts
gelesen, ausgeführt oder geändert. `sandbox-ai demo-device status|stop`.

## Sicherheitsmodi

Ein Sicherheitsmodus ist eine Voreinstellung der Optionen unten, von `lax`
bis `paranoid`. Der Modus setzt Vorgaben; Optionen dürfen ihn nur enger
machen. In `high` und `paranoid` wird eine Option abgelehnt, die die Sandbox
über den Modus hinaus öffnen würde; die Meldung nennt den Modus, woher er
kommt und den lockereren Modus, der die Option erlaubt:

```bash
./sandbox-ai claude --mode high            # kurz: -mo high
./sandbox-ai claude --mode high --allow-github
# agent-sandbox: --allow-github widens the security mode high (--mode): it opens hosts beyond the
# agent's provider ...; leave it out, or choose a laxer mode: --mode medium ...
```

| Modus | Netzwerk | Rückfragen | Workspaces, Mounts | Geräte, Anzeige | Weiteres |
|---|---|---|---|---|---|
| `lax` | jedes Ziel nach außen (`--egress-all-allow`), jede Verbindung vom Proxy protokolliert | aus (`--ask=off`) | wie angegeben | wie angegeben | `--log-debug`; [Köderdateien](#köderdateien-canaries) nur gemeldet, ein abfließender Token wird nicht abgewiesen |
| `low` | `--profile development --allow-npm`: Anbieter, PyPI, npm, GitHub, OpenAI- und Anthropic-APIs | an | wie angegeben | wie angegeben | |
| `medium` (Vorgabe) | nur der Anbieter des Agenten | an | wie angegeben | wie angegeben | die Vorgaben des Launchers: genau ein Start ohne `--mode` (Köder: `credentials`, Lesen gemeldet, ein abfließender Token abgewiesen und der Lauf gestoppt) |
| `high` | nur der Anbieter; Optionen öffnen nichts | an, Antworten höchstens für den Lauf | `/work` beschreibbar, weitere Workspaces nur lesbar, keine `rw`-Mounts | kein `--device`, `--gpu`, `--gui-wayland` | Datenschutzprüfung jedes Pushs (`--privacy-check=always`), 4g Speicher, 2048 Prozesse, CPUs höchstens die Vorgabe; ein Regelwerk fragt vor jedem Schreiben (POST, PUT, PATCH, DELETE) an einen anderen Host als den Anbieter, und sobald Inhalt eines solchen Hosts [dem Lauf einen Taint gab](#regelwerke), vor jedem Aufruf eines MCP-Werkzeugs; Köder `credentials` und `home`, nach einem Lesen wird gefragt (die Sandbox ist solange pausiert), ein abfließender Token stoppt den Lauf |
| `paranoid` | wie `high`, oder `--offline` | an, Antworten nur einmal | alle Workspaces nur lesbar (`--workspace-ro`): Ergebnisse gehen als Text oder Patch hinaus | wie `high`, dazu kein `--gui`/`--gui-vnc` | wie `high`, dazu: ein Regelwerk lehnt jede HTTP-Anfrage außer an den Anbieter ab, kein `-D`, kein MCP-Server mit Netzwerk, `--log-debug`, 2g Speicher, 2 CPUs, 1024 Prozesse; das Lesen eines Köders stoppt den Lauf; auch Ergebnisse von MCP-Werkzeugen und WebSearch und WebFetch des Agenten geben dem Lauf einen Taint, danach fragen auch seine Shell, Dateiänderungen und Abrufe nach (über [seine Hooks](#regelwerke)) |

Was „enger" heißt, Option für Option (in `high` und `paranoid`):

- **Netzwerk**: `--offline`, `--profile offline`, `--no-provider-defaults`,
  `--allow-http`, `--rules` und `--vpn` machen enger; jede Option, die ein Ziel
  öffnet, macht weiter: `--profile` (außer `offline`), `--egress-all-allow`,
  die Presets (`--allow-github` ...), `--frontier`, `--allow-host`, `--allow`,
  `--allow-tcp`, `--allow-host-port`, `--dns`, `--nameserver`, `--llm` und ein
  Git-Token (`--git-token-*`, `--git-credentials`), das seinen Server öffnet.
  In `lax` und `low` ersetzt eine Netzwerkwahl (`--profile`, `--offline`,
  `--egress-all-allow`, `--no-provider-defaults`, `--local-config`) das
  Netzwerk des Modus; andere Netzwerkoptionen kommen hinzu.
- **Proxy und Rückfragen**: `--no-proxy`, `--no-inspect` und `--ask=off`
  machen weiter; `--ask=on` macht `lax` enger. Der weiteste Geltungsbereich
  einer Antwort ist `run` (`high`) oder `once` (`paranoid`), oder
  `max_approval_scope` der Richtlinie, wenn das enger ist; unterhalb von
  `always` werden gemerkte Freigaben nicht angewandt.
- **Datenschutzprüfung**: `always` ist am strengsten, dann `public`, dann
  `off`; die strengen Modi nehmen nur `always`.
- **Workspaces und Mounts**: `--workspace-ro` und `ro`-Mounts machen enger,
  ein `rw`-Mount weiter.
- **Köderdateien**: `--no-canaries` macht beide strengen Modi weiter; ihre
  Kategorien (`credentials`, `home`) werden gelegt, was `--canaries` auch hinzufügt.
- **Geräte und Anzeige**: `--device`, `--gpu` und `--gui-wayland` machen in
  beiden strengen Modi weiter, `--gui` und `--gui-vnc` in `paranoid`.
- **Limits**: ein kleineres `--memory`, `--cpus` oder `--pids` macht enger, ein
  größeres oder `none` weiter.
- **Agent**: `-D` (`--dangerously-skip-permissions`) macht `paranoid` weiter;
  `--mcp NAME` ebenso, wenn der Server Netzwerk hat (ein Image-Server mit
  `"network": "full"`, ein Befehls-Server ohne `--offline`).
- **HOME**: `paranoid` bewahrt nichts über den Lauf hinaus auf: Jeder Start
  verwirft sein HOME am Ende (wie `--no-keep`), `--keep` und `--session`
  machen den Modus weiter, und ein aufbewahrtes HOME eines früheren Starts
  lässt sich darunter nicht zurückholen (`sandbox-ai resume` lehnt ab). `high`
  und die lockereren Modi bewahren HOMEs standardmäßig auf
  ([jeder Start](#jeder-start-namen-lösen-aufbewahren-fortsetzen)); die
  Prüfung des HOME vor jeder Wiedergeburt gilt für sie, und ein aufbewahrtes
  HOME behält seine Köder-Dateien (dieselben Dateien und Tokens) von Lauf zu
  Lauf.
- **Taint**: Die strengen Modi verschärfen sich, sobald nicht
  vertrauenswürdiger Inhalt in den Lauf gelangt ist (siehe *der Taint des
  Laufs* unter [Regelwerke](#regelwerke)). In `high` gibt Inhalt jedes Hosts
  außer dem Anbieter dem Lauf einen Taint, danach fragt jeder Aufruf eines
  MCP-Werkzeugs nach. In `paranoid` tun das auch jedes Ergebnis eines
  MCP-Werkzeugs sowie WebSearch und WebFetch des Agenten; WebSearch läuft beim
  Anbieter und nie durch den Proxy. Danach fragen auch die Shell, die
  Dateiänderungen und die Abrufe des Agenten nach, über seine Hooks.
  `sandbox-ai net untaint` nimmt den Taint zurück.

**Während des Laufs** gilt der Modus ebenso: der Start hält ihn in `run.json`
des Laufs und in der Konfiguration des Supervisors fest. In `high` und
`paranoid` öffnet `sandbox-ai net allow` nichts, was der Modus beim Start
ablehnt (`--allow`, `--allow-host`, `--allow-tcp`, Presets), und nennt den
Modus, der es ablehnt; der Supervisor lehnt eine solche Anfrage ebenfalls ab,
auch eine von Hand an den Steuer-Socket geschriebene. Antworten auf die
Rückfragen der Sandbox bleiben möglich, begrenzt auf den Geltungsbereich des
Modus (`run`, `once`), in `sandbox-ai approvals` und nochmals im Supervisor;
die HTTP-Regeln von `paranoid` lehnen weiterhin jede Anfrage außer an den
Anbieter ab.

**Woher der Modus kommt**, das Erste, was gesetzt ist: `--mode`, der Schlüssel
`"mode"` der [Projektdatei](#projektkonfiguration-und-wizard), `"mode"` in
`~/.config/sandbox-ai/defaults.json` (`{"version": 1, "mode": "high"}`),
sonst `medium`. Eine Projektdatei wählt ihren Modus mit dem Schlüssel, nicht
mit `--mode` in ihren Optionen. `min_mode` des Administrators
([Richtlinie](#richtlinie-des-administrators)) ist eine Untergrenze: ein Modus
darunter wird abgelehnt, wer ihn auch gewählt hat, und ohne Wahl ist die
Untergrenze der Modus. Optionen einer vertrauten Projektdatei zählen wie
Optionen auf der Kommandozeile. Die Sandbox eines MCP-Servers übergeht Ihren
Vorgabemodus (seine Definition entscheidet), nicht die Untergrenze der
Richtlinie. Der `--dry-run`-Plan zeigt den Modus unter `mode`: Name, Herkunft,
die hinzugefügten Optionen, den Geltungsbereich der Antworten, ob weitere
Workspaces nur lesbar sind und ob er ein Regelwerk mitbringt (unter
`rulesets` als `mode NAME`). `sandbox-ai doctor` nennt den Modus, den ein Start
im aktuellen Verzeichnis bekommt, und listet die Modi; `sandbox-ai wizard` fragt
zuerst nach dem Modus und bietet danach nur an, was er erlaubt.

## Workspaces und Mounts

`--workspace PFAD` erscheint als `/work`, dem Arbeitsverzeichnis des Agenten,
und ist schreibbar, sofern nicht `--workspace-ro` gesetzt ist. Relative Pfade
beziehen sich auf das Aufrufverzeichnis; fehlende Verzeichnisse werden
angelegt (ein vorhandenes erzeugt eine Warnung). `root:root` in der Sandbox ist
erwartet: Namespace-UID 0 entspricht Ihrem Benutzer, `chmod 777` ist unnötig.

Ein Workspace darf weder Ihr HOME, `/`, das Temp-Verzeichnis,
`XDG_RUNTIME_DIR` noch den Starter von sandbox-ai selbst sein oder enthalten,
und nicht innerhalb von `--state-dir` oder einem rw-`--mount` liegen: Der Agent
könnte Shell-Startdateien, Anmeldedaten oder den Starter ändern oder den Pfad
per Symlink umlenken. Ein rw-`--mount`, `--state-dir` oder der rw-Ordner eines
MCP-Servers darf den Starter von sandbox-ai (ein Checkout, ein venv) ebenfalls
weder sein, noch enthalten, noch in ihm liegen; geben Sie einen solchen Ordner
schreibgeschützt (`:ro`). Ein Workspace, ein rw-Mount oder `--state-dir` darf auch
`~/.config`, `~/.local`, `~/.ssh`, `~/.gnupg`, `~/.mozilla`, `~/bin` oder
`~/.bin` (die XDG-Verzeichnisse, wo immer sie gesetzt sind) weder sein, noch
enthalten, noch darin liegen, und kein Verzeichnis Ihres `PATH` sein oder darin
liegen; ausgenommen sind die HOMEs, die sandbox-ai selbst für Sessions,
MCP-Server, Logins und Plugins anlegt. sandbox-ai merkt sich jeden Baum, den
eine Sandbox schreiben konnte (`~/.local/state/sandbox-ai/exposed.json`); ein
Workspace, Mount, `--state-dir` oder `--log-dir`, dessen Pfad über einen Symlink
in einem solchen Baum führt, wird abgelehnt, weil ein Agent ihn angelegt haben
kann, um ein anderes Verzeichnis eingebunden zu bekommen. Ihre eigenen Links
anderswo (`~/src -> /data/src`) sind kein Problem. Ein Workspace darf ein
Verzeichnis Ihres `PATH` nur enthalten, wenn es das eigene `.venv/bin`,
`venv/bin`, `node_modules/.bin` oder `$VIRTUAL_ENV/bin` des Projekts ist; das
ist Bequemlichkeit, kein Schutz: Der Agent kann dort ein `git` ablegen, das Ihre
Shell als Nächstes ausführt, deshalb warnt sandbox-ai nach dem Lauf, wenn dort
Programme hinzugekommen oder geändert worden sind.
`.git/hooks` und `.git/config` eines Workspace führen
Code aus, wenn Sie auf dem Host git benutzen; nach einem Lauf also prüfen.

**Mehrere Workspaces.** `--workspace` wiederholen oder Einträge per Komma
trennen. Der **erste Eintrag ist immer `/work`**; jeder weitere erscheint unter
`/workspaces/<Verzeichnisname>` (`-2`, `-3` … bei gleichen Namen). Sie liegen
nicht in `/work`, damit im Haupt-Repository auf dem Host keine Mountpunkte
entstehen. Doppelte oder verschachtelte Workspaces werden abgelehnt;
`--workspace-ro` gilt für alle. Claude Code bearbeitet Dateien außerhalb seines
Arbeitsverzeichnisses erst nach Freigabe: `-- --add-dir /workspaces/<name>`
übergeben oder `--dangerously-skip-permissions` nutzen.

```bash
./sandbox-ai claude --workspace "$HOME/src/app,$HOME/src/shared-lib" --workspace "$HOME/notes"
#   /work = app, /workspaces/shared-lib, /workspaces/notes
```

**Ein Workspace, der während des Laufs hinzukommt.** `sandbox-ai workspace add
[LAUF] VERZ [--ro] [--name NAME]` hängt VERZ ohne Neustart unter `/workspaces/<name>` ein;
`workspace list` zeigt die Workspaces des Laufs, `workspace remove [LAUF]
VERZ|NAME` nimmt einen hinzugefügten wieder weg. VERZ wird geprüft wie
`--workspace` beim Start (kein HOME-, Temp- oder Laufverzeichnis, kein privater
Ort, kein Link, den eine Sandbox gelegt haben könnte, keine Überschneidung mit
Workspaces, Mounts und HOME des Laufs, keine der Dateien, die der Launcher liest,
darin, die Policy des Administrators) und ist schreibgeschützt mit `--ro` und
immer dann, wenn `--workspace-ro` oder der Sicherheitsmodus weitere Workspaces
schreibgeschützt hält. In der Sandbox läuft dafür nichts: Ein Helfer Ihres
Benutzers prüft, dass das geöffnete Verzeichnis das geprüfte ist (Gerät und
Inode, ein inzwischen untergeschobener Symlink hängt also nichts ein), betritt
den User-Namespace der Sandbox und hängt einen Klon davon in jeden ihrer
Mount-Namespaces ein, auch den des Agenten unter Firejail (Linux 5.2 oder neuer;
schreibgeschützt ab 5.12), nie in einen Namespace, den der Agent selbst angelegt
hat. Ein Verzeichnis, das eines Ihrer PATH-Verzeichnisse enthält (ein `.venv/bin`),
wird abgelehnt: damit starten, dann prüft der Launcher es nach dem Lauf. `remove`
friert die Sandbox kurz ein (wo ihre cgroup ihre eigene ist), lehnt ab, solange ein
Prozess dort darin arbeitet oder eine Datei offen oder eingeblendet hat, lehnt ab,
solange der Agent eine Kopie in einem eigenen User- und Mount-Namespace hält
(`unshare`), die die Mount-Tabellen zeigen, und hängt strikt aus, sodass auch der
Kernel ablehnt, solange ein Prozess in den eigenen Namespaces der Sandbox den Baum
noch hält; `--force` hängt ihn trotzdem aus, und was noch offen ist, bleibt
nutzbar, auch zum Schreiben, bis es geschlossen wird. Eine Kopie, die der Agent
ausgehängt hat und nur über einen offenen Deskriptor hält, zeigt keine Tabelle:
sicher nimmt einen Workspace nur ein Neustart weg. Einer
Session hinzugefügt, bleibt der Workspace in ihren Optionen, sodass `sandbox-ai
resume NAME --restart` ihn am selben Ort wieder hat; einer, der mit `--ro`
hinzukam, bleibt nicht (er käme beschreibbar zurück), außer der Lauf hält alle
weiteren Workspaces ohnehin schreibgeschützt. Das Datei-Log (`--log-dir`)
beobachtet ihn ab dann, und seit 0.1.78 nimmt die Push-Prüfung seine Commits mit den
Regeln des Laufs und denen seines Projekts an (der Befehl sagt es; wo sie es nicht kann,
bleiben seine Pushes zu einem öffentlichen Remote abgelehnt), und die Privacy-Prüfung
nach dem Lauf sieht ihn auch an. Läufe, die
0.1.75 oder älter gestartet hat, haben dafür keinen Platz: mit `resume NAME
--restart --workspace VERZ` neu starten.

`--name NAME` legt ihn unter `/workspaces/NAME` statt unter dem Namen seines
Verzeichnisses ab (1-64 Buchstaben, Ziffern, `_`, `.`, `-`, beginnend mit
Buchstabe oder Ziffer; abgelehnt, wenn ein Workspace des Laufs diesen Namen
hat oder ein Workspace, den die Session behält, ihn nach einem Neustart
zurückbekommt). Eine Session behält den Namen mit (`--workspace VERZ
--workspace-name NAME=VERZ` in ihren Optionen), ein Neustart legt ihn also an
denselben Ort. VERZ darf auch eine einzelne reguläre **Datei** sein: Sie wird
genauso geprüft (die Datei und die Ordner, in denen sie liegt: an einem privaten
Ort mit Warnung, in sandbox-ais eigenen Ordnern abgelehnt; ein Socket, FIFO oder
Gerät wird nie genommen) und genauso eingehängt, auf eine leere Datei, die der
Helfer unter `/workspaces/NAME` anlegt (der Kernel hängt eine Datei nur auf eine
Datei). Eine Datei gilt für den Lauf: Ein Start nimmt keine Datei als Workspace,
sie bleibt also nicht für die Session, und Datei-Log, Push-Prüfung und
Privacy-Prüfung nach dem Lauf sehen nur Verzeichnisse an. Ein Editor, der
speichert, indem er eine neue Datei schreibt und über die alte umbenennt, ändert
den Namen auf dem Host, nicht die eingehängte Datei: Die Sandbox behält die
Datei, die sie bekam (erneut hinzufügen). `workspace list --json` nennt für
jeden Workspace `kind` (`dir` oder `file`) und für einen hinzugefügten, ob er
`named` ist.

```bash
sandbox-ai workspace add claude-1a2b3c4d ~/src/shared-lib --ro
#   added /home/me/src/shared-lib to claude-1a2b3c4d at /workspaces/shared-lib (ro)
sandbox-ai workspace add claude-1a2b3c4d ~/data/2026 --name data
sandbox-ai workspace add claude-1a2b3c4d ~/notes/todo.md --ro
#   added the file /home/me/notes/todo.md to claude-1a2b3c4d at /workspaces/todo.md (ro)
sandbox-ai workspace remove claude-1a2b3c4d shared-lib
```

**Weitere Mounts:** `--mount QUELLE:ZIEL:ro|rw`, mehrfach möglich. Quellen
müssen existieren; Ziele müssen unter `/work`, `/mnt` oder `/opt` liegen.
Kommas, Socket- und Gerätedateien werden abgelehnt (Geräte siehe [Hardware](#hardware-geräte-und-gpus)). Sockets in eingebundenen
Verzeichnissen können weiterhin Hostdienste erreichbar machen; nur Nötiges
einbinden.

Claude und Codex fragen beim ersten Start in einem Verzeichnis, ob Sie ihm
vertrauen. Die Workspaces haben Sie selbst gewählt, daher trägt sandbox-ai
`/work` und jedes `/workspaces/NAME` im Sandbox-HOME als vertraut ein
(`hasTrustDialogAccepted` in `~/.claude.json`, `trust_level = "trusted"` in
`~/.codex/config.toml`); eine dort schon gespeicherte Entscheidung bleibt.

## Anmeldung und Zustand

Ohne Auth-Option werden die vorhandenen Anmeldedateien des Agenten privat
(Modus `0600`) ins Sandbox-HOME kopiert, sobald dort noch kein Login liegt
(das HOME eines neuen Starts immer; ein aufbewahrter Start, ein `--state-dir`
oder eine Session, die zurückkommen, behalten ihr eigenes); fehlende Dateien
verhindern den Start nicht. Quelle ist der
[Login-Speicher](#login-speicher-für-sandboxes), wenn er ein Login für den
Agenten hat, sonst die Hostdateien unten. Jede Kopie wird auf stderr mit Quelle und Ziel
gemeldet, nie mit Inhalt, und `--dry-run` listet geplante Kopien unter
`auth_copies`.

| Agent | Quelle auf dem Host | Ziel in der Sandbox |
|---|---|---|
| Codex | `~/.codex/auth.json` (`CODEX_HOME`) | `/root/.codex/auth.json` |
| Claude | `~/.claude/.credentials.json` (`CLAUDE_CONFIG_DIR`) | `/root/.claude/.credentials.json` |
| Claude-Metadaten | `~/.claude.json` (nur der Kontoteil) | `/root/.claude.json` |
| OpenCode | `~/.local/share/opencode/auth.json` (`XDG_DATA_HOME`) | `/root/.local/share/opencode/auth.json` |

| Option | Wirkung |
|---|---|
| `-ca`, `--copy-auth` | Kopie verlangen; fehlt die Hauptdatei, ist das ein Fehler. Ein Login in einem wiederverwendeten Sandbox-HOME bleibt erhalten. |
| `-na`, `--no-auth` | Nichts kopieren. |
| `-af`, `--auth-file DATEI` | Diese Datei an das Auth-Ziel des Agenten kopieren. |
| `-ic`, `--import-codex-auth DATEI` | Nur OpenCode: Codex-ChatGPT-OAuth in eine OpenCode-Anmeldung umwandeln. |
| `-cp`, `--copy QUELLE:ZIEL` | Beliebige Datei nach `ZIEL` relativ zum Sandbox-HOME kopieren. |
| `-S`, `--state-dir VERZ` | Dauerhaftes Sandbox-HOME (Ihnen gehörend, Modus `0700`, außerhalb des Workspace); nur ein Lauf zugleich (gesperrt). |

OS-Schlüsselbunde werden nicht exportiert: Ein Agent, der seine Anmeldung nur
im Schlüsselbund hält, braucht zuerst eine dateibasierte Anmeldung.

**Claude-Anmeldung.** Für Claude meldet der Start zusätzlich, wie lange der
kopierte Access-Token gültig ist. Ein abgelaufener Access-Token ist kein Grund
für eine neue Anmeldung: Claude erneuert ihn bei der ersten Anfrage mit dem
Refresh-Token. Sein Token-Endpunkt (`platform.claude.com`, bei älteren
Versionen `console.anthropic.com`) gehört zu Claudes Anbieter-Hosts und ist in
jedem Sicherheitsmodus offen, auch eine stundenlang untätige Sandbox erneuert
also. Anthropic rotiert OAuth-Refresh-Tokens; nur die zuletzt erneuernde Seite
hält ein gültiges Paar, und dieses Paar muss jeden anderen Inhaber des Logins
erreichen, bevor er mit dem verbrauchten Token erneuert. Bei automatischer
Kopie oder `--copy-auth` hält der Launcher deshalb die `.credentials.json` der
Sandbox und ihre Quelle (Host oder Login-Speicher) **schon während des Laufs**
im Gleichstand: Er sieht alle zwei Sekunden nach beiden Dateien (nur `lstat`)
und vergleicht sie, wenn sich eine geändert hat, unter einer Sperre, die alle
sandbox-ai-Prozesse des Hosts teilen
(`~/.local/state/sandbox-ai/claude-login.lock`):

- Die Sandbox hat erneuert und die Quelle hält noch das Login, das beide
  zuletzt geteilt haben: Das neue Paar geht sofort an die Quelle, eine danach
  gestartete Sandbox bekommt also dieses und nicht das verbrauchte.
- Die Quelle hält ein anderes Login (die Erneuerung einer anderen Sandbox,
  Claude auf dem Host, ein neues `sandbox-ai login claude`) und die Sandbox hat
  seitdem nicht erneuert: Es geht in die Sandbox. Claude liest seine
  Anmeldedatei vor dem Erneuern erneut und nimmt dann das neuere Paar, statt
  seinen verbrauchten Refresh-Token zu senden.
- Beide haben sich geändert: Die Quelle gewinnt. Durch diesen
  Compare-and-Swap ersetzt ein veraltetes Paar nie ein neueres.

Was geschehen ist, meldet der Launcher nach dem Lauf (währenddessen gehört das
Terminal dem Agenten), und derselbe Vergleich läuft am Ende noch einmal. Vor
dem Start ersetzt das Login der Quelle das eines wiederverwendeten
Zustandsverzeichnisses. Zurück geht nur ein Paar, das neuer ist als das der
Quelle, und nur, wenn die Quelle beim Start ein Login hatte. Es zählt
nur das Login, nicht die ganze Datei: Claude auf dem Host schreibt die Datei
auch aus anderen Gründen neu (MCP-OAuth-Tokens); würde das Zurückschreiben dann
verweigert, bliebe auf dem Host ein Refresh-Token, den die Sandbox schon
verbraucht hat, und der nächste Start endete mit „Login expired“. Wurde ein Lauf
mit dauerhaftem HOME (`--state-dir`, Sessions) vor dem Zurückschreiben
abgebrochen, erkennt der nächste Start das an einer privaten Liste von
Token-Prüfsummen (`~/.local/state/sandbox-ai/login-sync.json`, keine Tokens)
und schreibt das neuere Paar zuerst zurück; ein Lauf ohne geteiltes Login
(`--no-auth`, anderes Konto) löscht diesen Eintrag, sein Login wird also nie
dafür gehalten. Von `.credentials.json` gelangt nur das `claudeAiOauth`-Login in
die Sandbox, nicht die OAuth-Tokens Ihrer MCP-Server. Die Hostdateien werden
nur gelesen, wenn sie außerhalb aller Workspaces, rw-Mounts und `--state-dir`
liegen (auch über `CODEX_HOME`, `CLAUDE_CONFIG_DIR` und `XDG_DATA_HOME`). Nur typgeprüfte Token-Felder
(`accessToken`, `refreshToken`, `expiresAt`, `refreshTokenExpiresAt`) gelangen
zurück, atomar mit Modus `0600`, und nur aus einer regulären, größenbegrenzten
Datei, die kein Symlink ist, mit einem Ablauf höchstens 31 Tage in der Zukunft;
Symlinks in einem wiederverwendeten Sandbox-HOME werden abgelehnt. Während der
Agent läuft, wird die Sandbox-Datei über Verzeichnis-Deskriptoren gelesen und
ersetzt, ohne einem Link zu folgen, denn der Agent kann sein HOME jederzeit
ändern. Tokens werden nie ausgegeben oder protokolliert; die Sperre und
`login-sync.json` enthalten keine. Ein fehlgeschlagenes Zurückschreiben wird
gemeldet, ändert aber nie den Exit-Status des Agenten. Restrisiken: Zwei
Inhaber eines Logins, die in denselben zwei Sekunden erneuern, konkurrieren
weiterhin um seinen Refresh-Token; ein kompromittierter Agent könnte Tokens
eines anderen Kontos einschleusen, die die Quelle dann nutzt. `--no-auth` oder `--auth-file` lassen die Hostdatei unberührt.
Hostdateien anderer Agenten werden nie geschrieben.

**Vor dem Start** prüft der Launcher das Login, das die Sandbox bekommt. Fehlt
es, sagt er das (`sandbox-ai login AGENT`); ist Claudes Refresh-Token
abgelaufen, fragt er im Terminal, ob jetzt in einer Sandbox angemeldet werden
soll; dieses Login landet im Login-Speicher für alle späteren Sandboxes.

### Login-Speicher für Sandboxes

Teilen sich Host und Sandboxes ein Login, teilen sie auch dessen rotierenden
Refresh-Token: Wer als Zweiter mit demselben Token erneuert, ist abgemeldet.
Bei Claude gibt der Launcher jede Erneuerung schon während des Laufs weiter
(siehe oben), Codex- und OpenCode-Logins werden dagegen erst am Ende eines
Laufs zurückgeschrieben. `sandbox-ai login AGENT` gibt den Sandboxes ein
eigenes Login:

```bash
sandbox-ai login claude            # startet `claude auth login` in einer Sandbox; angezeigten Link öffnen
sandbox-ai login codex             # `codex login --device-auth` (kein localhost-Callback nötig)
sandbox-ai login opencode          # `opencode auth login`, mit freigegebenen Frontier-Hosts
sandbox-ai login claude -- auth login --console   # andere Login-Argumente nach --
sandbox-ai login --status          # welches Login die Sandboxes jedes Agenten bekommen, und sein Zustand
sandbox-ai login claude --logout   # vergessen; Sandboxes nutzen wieder das Host-Login
```

Die Anmeldung läuft in einer Sandbox mit frischem HOME, ohne kopierte
Anmeldedaten, ohne Projektkonfiguration und mit den Anbieter-Hosts des Agenten;
Launcher-Optionen wie `--backend bwrap` oder `--podman-url URL` werden
weitergegeben. Danach wird aus diesem HOME nur das Login übernommen (bei Claude
der `claudeAiOauth`-Block und der Kontoteil von `.claude.json`), ohne Symlinks
zu folgen, und in `~/.local/share/sandbox-ai/auth/AGENT/` abgelegt
(Verzeichnisse `0700`, Dateien `0600`). Ab dann bekommt jede Sandbox dieses
Agenten ihr Login von dort, und erneuerte Tokens gehen dorthin zurück; das Login
des Hosts wird weder benutzt noch geändert. Bei Codex und OpenCode gehen nur die
erneuerten Tokens des gespeicherten Kontos zurück (Codex: `access_token`,
`refresh_token`, `id_token` mit gleicher `account_id`; OpenCode: OAuth-Tokens
bereits gespeicherter Anbieter), nie ein API-Key, ein anderes Konto oder ein
neuer Anbieter; ein Speicher ohne die Struktur des Agenten wird ignoriert. Ein
dauerhaftes HOME (`--state-dir`, Session), das noch das Login aus dem Speicher
hält, übernimmt neuere Tokens, die eine andere Sandbox zurückgeschrieben hat. Ein
Workspace, der den Speicher enthält, wird abgelehnt. Restrisiko: Die Sandboxes
eines Agenten teilen sich das gespeicherte Login, ein Agent könnte es also für
spätere Sandboxes ersetzen. Zwei gleichzeitig laufende Codex- oder
OpenCode-Sandboxes konkurrieren weiterhin um einen Refresh-Token, Claude-Sandboxes
nur, wenn sie in denselben zwei Sekunden erneuern. `sandbox-ai login --status`
zeigt, wie lange ein Claude-Login noch erneuert werden kann, sofern Claude die
Angabe des Anbieters dazu gespeichert hat (`refreshTokenExpiresAt`).

## Kits, Projekt-Memory und Plugins

Jede Sandbox kann Skills, Agents, Commands und Prompts des Agents mitbringen,
das Memory des Agents pro Projekt behalten und Plugins aktivieren, ohne das
eigene `~/.claude`, `~/.codex` oder `~/.config/opencode` des Hosts
preiszugeben.

**Kits.** Ein Kit ist ein Verzeichnis mit einem Unterverzeichnis pro Agent:

```text
~/.local/share/sandbox-ai/
  home/claude/                  Standard-Kit: in jeder Claude-Sandbox
    skills/sandbox-example/SKILL.md
    agents/  commands/  CLAUDE.md  marketplaces.json
  home/codex/                   skills/  prompts/  AGENTS.md
  home/opencode/                skills/  agents/  commands/  AGENTS.md  marketplaces.json
  kits/review/claude/           --kit review
  kits/review/codex/
```

| Im Kit | Claude | Codex | OpenCode |
|---|---|---|---|
| `skills/NAME/SKILL.md` | `~/.claude/skills` | `~/.agents/skills` | `~/.config/opencode/skills` |
| `agents/NAME.md` | `~/.claude/agents` | — | `~/.config/opencode/agents` |
| `commands/NAME.md` | `~/.claude/commands` | — | `~/.config/opencode/commands` |
| `prompts/NAME.md` | — | `~/.codex/prompts` | — |
| Memory-Vorlage | `CLAUDE.md` | `AGENTS.md` | `AGENTS.md` |
| `marketplaces.json` | Marketplaces und Plugins | — (noch nicht) | npm-Plugins |
| `hooks/` ([nach Freigabe](#hooks-in-kits)) | `hooks.json` und die Skripte, die es startet | `hooks.json` und die Skripte, die es startet | Plugin-Module (`*.js`, `*.mjs`, `*.ts`) |

Der erste Start eines Agents legt sein Standard-Kit aus den Vorlagen von
sandbox-ai an: mit einem Beispiel-Skill (`sandbox-example`, erklärt dem Agent
die Sandbox), einer Memory-Vorlage und für Claude dem offiziellen
Anthropic-Marketplace und dem Demo-Plugin von sandbox-ai. Ändere oder lösche
sie; ein vorhandenes Standard-Kit wird nie wieder verändert, ein gelöschtes
Beispiel bleibt gelöscht.

Jede Sandbox bekommt außerdem den **eingebauten Skill** `sandbox-ai`
(in `sandbox/builtin/AGENT`, wird mit sandbox-ai aktualisiert): wie der Agent
in der Sandbox arbeitet, was geteilt ist, wie Netz, Proxy,
Datenschutzprüfung, Hardware, Displays und Sessions sich verhalten und welche
Startoption oder welchen `sandbox-ai net`-Befehl der Mensch wofür braucht.
Ein Skill namens `sandbox-ai` in einem Ihrer Kits ersetzt ihn.

```bash
sandbox-ai claude                             # nur das Standard-Kit
sandbox-ai claude --kit review --kit python   # dazu zwei benannte Kits
sandbox-ai codex --kit-dir ~/meine-kits       # dazu ~/meine-kits/codex
sandbox-ai claude --no-default-kit --kit review
```

Die Einträge aller Kits werden zusammengeführt. Derselbe Skill-, Agent-,
Command- oder Prompt-Name aus zwei Kits oder zwei Memory-Vorlagen beenden den
Start mit einer Meldung, die beide Kits nennt; nichts wird still ersetzt.
sandbox-ai kopiert die Einträge in einen privaten Schnappschuss und hängt ihn
**nur lesbar** ein: Der Agent kann sie nutzen, nicht ändern.
Ein Kit-Eintrag darf ein Symlink sein (praktisch für einen Skill aus einem
Git-Checkout); unterhalb eines Eintrags wird kein Link verfolgt (die Kopie
enthält die eigenen Dateien des Eintrags). Weder ein Kit noch ein Link-Ziel darf in einem Workspace, einem
rw-Mount oder dem `--state-dir` liegen, und keines davon in einem Kit oder
Store. Ein Kit-Eintrag, eine Memory-Vorlage, eine `marketplaces.json` oder ein
`--kit-dir` wird Link für Link verfolgt, und der Start hält an einem Link an,
der in einem Ordner liegt, den eine Sandbox schreiben kann oder konnte (ein
Workspace dieses oder eines früheren Laufs, einer gerade laufenden Sandbox oder
einer behaltenen Sitzung): Ein Agent, der in Ihrem Checkout arbeitet, könnte den
Ordner des Skills durch einen Link auf `~/.ssh` ersetzt haben. Die Meldung nennt
diesen Link; prüfen Sie ihn und lassen Sie das Kit dann auf den echten Ordner
zeigen oder kopieren Sie den Skill ins Kit (`sandbox-ai kits add-skill`). Ein
Link auf einen echten Ordner in so einem Checkout funktioniert weiter, mit dem
Hinweis, dass alles, was ein Agent dort schreibt, jede Sandbox mit diesem Kit
erreicht. Schleifen, mehr als 5000 Dateien oder 64 MiB beenden den
Start. Einträge, die nur ein anderer Agent kennt (z. B. `prompts/` in einem
Claude-Kit), werden gemeldet und ignoriert. Namen für `--kit`: Buchstaben,
Ziffern, `_`, `.`, `-`.

**Projekt-Memory.** Die Memory-Datei des Agents (`~/.claude/CLAUDE.md`,
`~/.codex/AGENTS.md`, `~/.config/opencode/AGENTS.md`) und Claudes Auto-Memory
(`~/.claude/projects/-work/memory/`) bleiben pro Projekt erhalten (Projekt =
erstes Workspace), in `~/.local/state/sandbox-ai/memory/NAME-HASH/AGENT/`. Der
erste Lauf eines Projekts beginnt mit der Memory-Vorlage des Kits; danach
gehört die Datei dem Projekt. Der Agent arbeitet auf einer Kopie; nach dem Lauf
werden die Dateien, die er geändert oder gelöscht hat, ins Projekt-Memory
übernommen, nie in ein Kit oder das eigene Memory des Hosts. Symlinks,
Spezialdateien und mehr als 1000 Dateien oder 16 MiB werden nicht übernommen.
Zwei Sandboxes eines Projekts behalten beide ihre Änderungen: Geschrieben
werden nur Dateien, die ein Lauf geändert hat, und eine gelöschte Datei wird
nur entfernt, wenn sie inzwischen niemand geändert hat. Dabei folgt sandbox-ai
im HOME keinem Symlink, den der Agent gelegt hat, auch keinem, der während des
Kopierens untergeschoben wird. Stirbt der Launcher vor dem Sichern, behält ein
persistentes HOME (ein aufbewahrter Start, `--state-dir`, Session) die Änderungen
für den nächsten Start; ein am Ende verworfenes HOME (`--no-keep`) verliert sie. `--no-memory` startet ohne
Projekt-Memory; `sandbox-ai doctor` zeigt das Memory des Projekts im aktuellen
Verzeichnis.

**Claude-Plugins.** Marketplaces und Plugins stehen in `claude/marketplaces.json`
eines Kits, im Format von Claudes `extraKnownMarketplaces` plus einer
Plugin-Liste:

```json
{
  "marketplaces": {
    "claude-plugins-official": {"source": {"source": "github", "repo": "anthropics/claude-plugins-official"}},
    "sandbox-ai": {"source": {"source": "directory", "path": "/sandbox/marketplace"}}
  },
  "plugins": ["sandcastle@sandbox-ai"]
}
```

Quellen: `github` (`repo`), `git` und `url` (nur `https://`) sowie der eigene
Marketplace von sandbox-ai (`/sandbox/marketplace`). `sandbox-ai plugins
claude` installiert alles, was das Standard-Kit und alle Kits deklarieren, in
den Plugin-Store `~/.local/share/sandbox-ai/plugins/claude`: Es führt `claude
plugin marketplace add` und `claude plugin install` in einer Sandbox ohne
Login, ohne Projekt und mit Netz nur zum Host des Marketplaces aus
(GitHub-Preset bzw. Host einer `git`/`url`-Quelle). Jede Claude-Sandbox bekommt
danach `cache` und `marketplaces` des Stores nur lesbar und aktiviert genau die
Plugins, die ihre Kits nennen; ein für ein anderes Kit installiertes Plugin
bleibt aus. Marketplaces aktualisieren sich in der Sandbox nicht selbst; das
macht `sandbox-ai plugins claude --update`.

```bash
sandbox-ai plugins claude             # installieren, was die Kits deklarieren
sandbox-ai plugins claude --update    # auch Marketplaces und Plugins aktualisieren
sandbox-ai plugins claude --kit egm   # auch, was das eingebaute Kit egm deklariert
sandbox-ai plugins --status           # deklariert vs. installiert, pro Kit
sandbox-ai plugins claude -- list     # beliebiger `claude plugin ...`-Befehl auf dem Store
```

Das Demo-Plugin `sandcastle@sandbox-ai` bringt `/sandcastle`: ein Roboter, der
in seiner Sandbox mit Förmchen und Eimer Sandburgen baut. Eine Claude-Sandbox,
die vor `sandbox-ai plugins claude` startet, nennt die fehlenden Plugins.

Der Marktplatz von sandbox-ai bietet außerdem **`egm@sandbox-ai`** an, Googles
[Elephant-Goldfish Model](https://github.com/google/elephant-goldfish-model)
(Apache-2.0): Skills und Reviewer-Subagenten, mit denen der Agent ein
Design-Dokument entwirft und von Subagenten ohne den Gesprächskontext prüfen
lässt (`/egm:egm-brainstorm`, `/egm:egm-draft`, `/egm:egm-review`). Es ist aus,
solange kein Kit es nennt. sandbox-ai liefert das **eingebaute Kit `egm`** mit,
das es nennt (in `sandbox/builtin/kits/egm/claude`; ein eigenes Kit `egm`
ersetzt es), es braucht also keine eigene Datei:

```bash
sandbox-ai plugins claude --kit egm   # installieren: holt den gepinnten Commit von GitHub
sandbox-ai claude --kit egm              # aktivieren (oder Kit egm in sandbox-ai wizard edit, Teil k)
```

sandbox-ai pinnt es auf einen Upstream-Commit; die Installation holt diesen
Commit von GitHub (ein einfaches `sandbox-ai plugins claude` lässt eingebaute
Kits aus und geht für sie nie zu GitHub). Es nutzt Modell und Login der
Sandbox, keinen weiteren Dienst und keinen Schlüssel. Für jede Claude-Sandbox:
`"egm@sandbox-ai"` in `plugins` der `claude/marketplaces.json` des
Standard-Kits eintragen. Das Kit gibt es nur für Claude: Das Codex-Plugin von
Upstream braucht Plugin-Unterstützung für Codex, die sandbox-ai noch nicht hat
(ein Codex-Kit-Skill erreicht die Personas und Dokumente nicht, die die Skills
aus dem Plugin-Verzeichnis laden), und der OpenCode-Adapter ist experimentell,
an eine OpenCode-Version gebunden und wird in eine eigene Konfiguration
installiert statt als npm-Plugin.

Das eingebaute Kit **`superpowers`** aktiviert `superpowers@sandbox-ai`,
[obra/superpowers](https://github.com/obra/superpowers) 7.0.2 (MIT), gepinnt
auf den Commit seines Tags: Skills für Brainstorming, Pläne schreiben und
ausführen, testgetriebene Entwicklung, systematisches Debugging, Code-Review,
Git-Worktrees und Arbeit mit Subagenten. Sein SessionStart-Hook (er liest den
Skill `using-superpowers` des Plugins und gibt ihn als Kontext aus, sonst
nichts) läuft erst, wenn Sie ihn freigeben:

```bash
sandbox-ai plugins claude --kit superpowers   # den gepinnten Commit installieren
sandbox-ai kits trust-hooks superpowers       # seinen Hook prüfen und freigeben
sandbox-ai claude --kit superpowers
```

Nur für Claude, aus denselben Gründen wie egm. Weitere gewünschte Werkzeuge
(MCP-Server, Kits, Dienste) und wo sie stehen:
[INTEGRATIONS_DE.md](INTEGRATIONS_DE.md).

**OpenCode** bekommt die npm-Pakete aus `opencode/marketplaces.json`
(`{"plugins": ["name@1.2.3"]}`) als `plugin`-Einstellung; OpenCode installiert
sie selbst, dafür braucht es `--allow-host registry.npmjs.org`. Plugins für
**Codex** werden noch nicht unterstützt.

Kits, Memory und Plugins entfallen bei `--probe`, in MCP-Server-Sandboxes und in
der Login-Sandbox. `--copy` in einen Pfad, den sie füllen, wird abgelehnt, ein
Kit oder Store in einem Workspace ebenso. Restrisiko: Ein Plugin führt in jeder
Sandbox, die es aktiviert, Code aus (Hooks, MCP-Server); installiere nur
Plugins, denen Sie vertrauen.

### Kits verwalten: `sandbox-ai kits`

`sandbox-ai kits` zeigt alle Kits und ändert Ihre eigenen, ohne dass Sie
Dateien von Hand bearbeiten:

```bash
sandbox-ai kits                                  # alle Kits und was sie mitbringen, pro Agent
sandbox-ai kits show review                      # ein Kit (auch: default, built-in, ein eingebautes Kit)
sandbox-ai kits new review                       # kits/review/AGENT/skills; --agent A für weniger Agents
sandbox-ai kits add-skill review ~/skills/lint   # Skill-Ordner (SKILL.md oben) ins Kit kopieren; --as NAME
sandbox-ai kits new-skill review lint-fix --description "Fixes lint errors; use after edits"
sandbox-ai kits add-plugin review sandcastle@sandbox-ai           # deklariert auch den Marketplace von sandbox-ai
sandbox-ai kits add-plugin review tool@team --source team/plugins # deklariert den Marketplace team (GitHub)
sandbox-ai kits add-plugin review some-plugin@1.2.3 --agent opencode
sandbox-ai kits add-marketplace review team https://git.example.org/team/plugins.git
sandbox-ai kits import-marketplace review acme --with-plugins     # einen, den Claude hier schon kennt, mit Plugins
sandbox-ai kits disable-plugin review lint@acme  # deklariert (und installiert), aber aus; enable-plugin schaltet ein
sandbox-ai kits remove-marketplace review acme --with-plugins
sandbox-ai kits add-plugin default tool@team     # auch Plugins und Marktplätze des Standard-Kits ändern sich
sandbox-ai kits remove-skill review lint-fix     # remove-plugin review PLUGIN ebenso
sandbox-ai kits add-hook review ~/hooks/check.sh --matcher Bash   # ein PreToolUse-Hook (--event, --timeout)
sandbox-ai kits trust-hooks review               # die Hooks des Kits prüfen, dann freigeben
sandbox-ai kits delete review --yes
```

`list --json` und `show NAME --json` geben die Übersicht für Programme aus (die
Desktop-App liest sie): jedes Kit mit seiner Art (`always` für den eingebauten
Skill, `default`, `builtin`, `user`), seinem Pfad und ob `sandbox-ai kits` es
ändern darf; pro Agent Skills, Agents, Commands, Prompts, Memory-Vorlage,
Marketplaces, Plugins (aktivierte und `disabled`: deklariert, aber aus), Hinweise und Fehler; den Marketplace von sandbox-ai mit
den Kits, die jedes Plugin nennen; und was der Plugin-Store enthält. Ein
beschädigtes Kit wird für diesen Agent gemeldet und hält die Liste nicht auf.

Geändert werden nur Ihre Kits in `~/.local/share/sandbox-ai/kits` und die
Plugins und Marktplätze des Standard-Kits (die Plugin- und Marktplatz-Befehle
nehmen `default`; fehlt es, wird es zuerst aus seinen Beispielen angelegt). Die
eingebauten Kits und der eingebaute Skill von sandbox-ai sind nur lesbar (ein
eigenes Kit gleichen Namens ersetzt ein eingebautes Kit für seine Agents), den
Rest des Standard-Kits bearbeiten Sie von Hand. Nichts wird über einen Symlink geändert:
Ein Kit, Agent-Verzeichnis, `skills/`-Verzeichnis oder `marketplaces.json`, das
ein Symlink ist (etwa in einen Checkout), wird abgelehnt; ändern Sie es dort,
wo es liegt. `delete` und `remove-skill` entfernen bei einem solchen Link nur
den Link. `add-skill` macht einen **Schnappschuss**: Der Ordner selbst darf
kein Symlink sein; darin werden Symlinks, FIFOs, Sockets und Geräte mit einem
Hinweis übersprungen, nie verfolgt oder geöffnet (auch `.git` bleibt draußen);
die Kit-Grenzen (5000 Dateien, 64 MiB, 16 Ebenen) gelten, und die Kopie
erscheint erst, wenn sie vollständig ist. Liegt der Ordner in einem Workspace
oder einem anderen Ordner, den eine Sandbox schreiben kann oder konnte, sagt
der Befehl das: Prüfen Sie die Kopie, ein Agent kann sie geschrieben haben.
`marketplaces.json` wird wie beim Start geprüft und atomar geschrieben;
`$comment` und andere Einträge bleiben. Eine Marketplace-Quelle ist
`OWNER/REPO` (GitHub), `https://...` (Git oder die URL einer
`marketplace.json`) oder `sandbox-ai`; ein Ordner auf diesem Rechner wird
abgelehnt, weil Sandboxes nur den eigenen Marketplace von sandbox-ai als
Verzeichnis bekommen: Veröffentlichen Sie Ihren auf einem Git-Server oder
GitHub. Plugins für Codex werden noch nicht unterstützt.

### Die Plugin-Marktplätze: `sandbox-ai plugins marketplaces`

`sandbox-ai plugins marketplaces` listet jeden Plugin-Marktplatz auf diesem
Rechner mit seinen Plugins und dem Zustand jedes Plugins: den eigenen von
sandbox-ai, die, die Kits deklarieren (Ihre, die des Standard-Kits, die der
eingebauten Kits), die im Plugin-Store von sandbox-ai, die Vorschläge des
Standard-Kits und die, die die Agents selbst schon kennen:

- **Claude**: `~/.claude/plugins/known_marketplaces.json` (mit den Kopien, die
  Claude geholt hat), `extraKnownMarketplaces` und `enabledPlugins` in
  `~/.claude/settings.json`, `~/.claude/plugins/installed_plugins.json`
  (`CLAUDE_CONFIG_DIR` gilt).
- **Codex**: `[marketplaces.NAME]` und `[plugins."NAME@MARKTPLATZ"] enabled` in
  `~/.codex/config.toml`, die Kopien unter `~/.codex/.tmp/marketplaces`, der
  kuratierte Marktplatz unter `~/.codex/.tmp/plugins` und die installierten
  Plugins unter `~/.codex/plugins/cache` (`CODEX_HOME` gilt). Die Kits von
  sandbox-ai geben Codex noch keine Plugins; sie werden daher so gezeigt, wie
  Codex sie hat. Ein Git-Marktplatz, dessen Kopie im Format von Claude ist, lässt
  sich für Claude importieren.
- **OpenCode** hat keine Marktplätze: Seine Plugins sind npm-Pakete (`plugin` in
  `opencode.json` oder `opencode.jsonc`), gelistet mit den Kits, die sie nennen,
  dazu die Plugin-Module in seinem Ordner `plugin/` (ein Kit nimmt eines als
  Hook).

```bash
sandbox-ai plugins marketplaces                 # jeder Marktplatz, seine benutzten Plugins
sandbox-ai plugins marketplaces acme            # ein Marktplatz mit jedem Plugin und seiner Beschreibung
sandbox-ai plugins marketplaces --agent codex   # nur die von Codex; --json für Programme (Web und App)
sandbox-ai plugins claude --marketplaces-only   # die Marktplätze der Kits holen, damit ihre Plugin-Listen erscheinen
```

Die Liste holt, startet und ändert nichts, auch nicht die Konfiguration der
Agents: Deren Dateien werden nur gelesen (eine reguläre Datei, höchstens 4 MiB,
die Liste eines Marktplatzes höchstens 16 MiB; eine fehlende oder beschädigte
ist ein Hinweis, nie ein Fehler). Eine Plugin-Liste stammt aus einer früher
geholten Kopie (der von sandbox-ai, der des Plugin-Stores oder der des Agents);
`sandbox-ai plugins claude --marketplaces-only` holt die Marktplätze der Kits in
der Sandbox des Plugin-Stores, die nur deren Hosts erreicht, und installiert
nichts. Was Sie auswählen, ändert ein Kit mit den Befehlen oben: `add-plugin`,
`remove-plugin`, `enable-plugin`, `disable-plugin`, `add-marketplace`,
`remove-marketplace` und `import-marketplace KIT MARKTPLATZ [--from
claude|codex] [--with-plugins]`, das einen Marktplatz, den der Agent kennt, mit
der Quelle deklariert, die er dort hat (`--with-plugins`: und die Plugins, die
der Agent daraus aktiviert). Ein Marktplatz in einem Ordner auf diesem Rechner
kann nicht in ein Kit (die Liste sagt, warum).

Die `marketplaces.json` eines Kits listet unter `plugins`, was es aktiviert, und
unter `disabled`, was es deklariert, aber ausgeschaltet hält: `sandbox-ai
plugins claude` installiert beides, ein Start aktiviert nur das Erste. Von den
Kits eines Starts entscheidet das letzte, das ein Plugin nennt; so kann ein Kit,
das mit `--kit` kommt, ein Plugin ausschalten, das das Standard-Kit aktiviert
(`sandbox-ai kits disable-plugin KIT PLUGIN`).

Die Seite Kits im Web ist ein Browser darauf: einen Marktplatz wählen, seine
Plugins mit ihrem Zustand sehen, eines zu einem Kit hinzufügen, es dort
aktivieren, deaktivieren oder entfernen, einen Marktplatz hinzufügen oder
entfernen oder einen importieren, den Claude oder Codex auf diesem Host kennt.

### Hooks in Kits

Ein Kit kann Hooks des Agenten in `AGENT/hooks/` mitbringen. Sie laufen erst,
nachdem Sie sie geprüft und freigegeben haben, und nur, solange sich nichts
an ihnen ändert.

- **Claude Code und Codex:** `hooks.json` enthält die Hooks im Format des
  `"hooks"`-Objekts der Einstellungen von Claude Code, daneben liegen die
  Skripte, die sie starten. Ein Befehl nennt eine Datei des Ordners als
  `${HOOK_DIR}/DATEI`:

  ```json
  {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
    {"type": "command", "command": "${HOOK_DIR}/check.sh", "timeout": 30}]}]}}
  ```

  Aus einem Kit laufen nur Hooks mit `"type": "command"`.
- **OpenCode:** Plugin-Module (`*.js`, `*.mjs`, `*.ts`) oben im Ordner, mit
  ihren Hilfsdateien daneben.

`sandbox-ai kits add-hook KIT DATEI` fügt einen Hook hinzu:

- ein Skript, mit `--event` (Standard `PreToolUse`), `--matcher` (Standard:
  jedes Werkzeug) und `--timeout`;
- eine `.json`-Datei mit Hook-Definitionen;
- ein OpenCode-Plugin-Modul.

`remove-hook KIT DATEI` nimmt einen Hook wieder heraus. `kits show KIT` nennt
jeden Hook (Ereignis, Matcher, Befehl, die Dateien, die er startet) und ob er
freigegeben ist. `list --json` und `show --json` enthalten dasselbe unter
`"hooks"`: `hooks`, `files` (mit Größe, SHA-256 und Ausführbar-Bit),
`sha256`, `trusted`, `notes` und `errors`.

**Freigabe.** `sandbox-ai kits trust-hooks KIT` zeigt jeden Hook und den
vollen Text jeder Datei im Ordner. Nach Ihrer Bestätigung (oder mit `--yes`)
hält es den SHA-256 des ganzen Ordners fest: Pfad, Inhalt und Ausführbar-Bit
jeder Datei. KIT ist der Name eines Kits, `default` oder ein
`--kit-dir`-Ordner. `--revoke` nimmt die Freigabe zurück.

- Freigaben liegen in `~/.config/sandbox-ai/trusted-hooks.json`, neben Ihren
  Regeln. Ein Start lehnt ein Konfigurationsverzeichnis ab, das eine Sandbox
  schreiben kann.
- Jede Änderung am Ordner nimmt die Freigabe weg, egal wer sie macht.
- Eine Freigabe gilt nur für ihr Kit. Dieselben Hooks in einem anderen Kit,
  etwa im `--kit-dir` eines Projekts, brauchen eine eigene Freigabe.
- Ein Symlink oder eine Spezialdatei im Ordner heißt: Seine Hooks laufen nie
  und lassen sich nicht freigeben.
- Nichts in einem Kit oder einer Projektdatei gibt etwas frei: Eine
  Projektdatei mit einer eigenen Freigabe wird wie jeder unbekannte Schlüssel
  abgelehnt.

**Beim Start** wird der Hook-Ordner jedes Kits in den Lauf kopiert, und die
Kopie wird gehasht: Es läuft genau das, was freigegeben wurde, auch wenn sich
das Kit während des Starts ändert. Hooks ohne Freigabe laufen nicht. Der
Start nennt sie zusammen mit dem Befehl, der sie freigibt:

```text
agent-sandbox: kit review: 1 hook(s) for claude not approved (new, or changed since they were
approved): they do not run; review and approve them with: sandbox-ai kits trust-hooks review --agent claude
```

Freigegebene Hooks werden Teil der verwalteten Konfiguration des Agenten,
derselben Stelle wie die [Hooks der Regeln für seine eigenen
Werkzeuge](#regelwerke):

- Claude Code: `/etc/claude-code/managed-settings.json`;
- Codex: `/etc/codex/requirements.toml`. Ein Codex älter als 0.131.0 würde
  sie übergehen, deshalb wird der Start abgelehnt, mit oder ohne
  Werkzeugregeln;
- OpenCode: seine Plugin-Liste.

`${HOOK_DIR}` wird zur schreibgeschützten Kopie im Lauf. Die Regel-Hooks
kommen zuerst, die Hooks der Kits folgen in Kit-Reihenfolge. Kit-Hooks laufen
mit oder ohne Werkzeugregeln: ein Mechanismus. Die eigenen Einstellungen des
Agenten starten dann keine Hooks (`allowManagedHooksOnly`,
`allow_managed_hooks_only`).

Auch eine umgeschriebene Werkzeugeingabe prüfen die Regeln. Ein Kit-Hook
kann mit `updatedInput` antworten, einer neuen Eingabe für das Werkzeug. Mit
Werkzeugregeln läuft deshalb jeder `PreToolUse`-Hook eines Kits über einen
Wrapper (`runtime.py --internal-kit-hook`). Der Wrapper startet den Hook
innerhalb seines eigenen Timeouts und fragt die Regeln nach der neuen
Eingabe, bevor der Agent sie bekommt. Lehnen die Regeln sie ab, wird der
Aufruf blockiert. Eine Antwort, die `updatedInput` nennt, sich aber nicht
lesen lässt, wird ebenfalls abgelehnt. In OpenCode läuft das Regel-Plugin
nach den Plugins der Kits ein zweites Mal. Dieser zweite Lauf fragt nur für
einen Aufruf erneut, dessen Argumente ein Plugin geändert hat.

**Hooks von Claude-Plugins.** Die eigenen Hooks eines Plugins
(`hooks/hooks.json`, oder `"hooks"` in `.claude-plugin/plugin.json`) laufen
nicht, solange die verwalteten Hooks an sind.
`sandbox-ai plugins claude trust-hooks NAME@MARKTPLATZ` gibt sie wie die
Hooks eines Kits frei. Es zeigt jeden Hook und den vollen Text der Dateien,
die seine Befehle nennen, und hält den SHA-256 des ganzen installierten
Plugin-Ordners fest, Links eingeschlossen (ihre Ziele, denen nie gefolgt
wird). Der Start setzt sie dann hinter die Hooks der Kits, mit
`CLAUDE_PLUGIN_ROOT`, wie Claude Code es setzt. Nur Hooks vom Typ
`"command"` lassen sich freigeben. Ohne verwaltete Hooks startet Claude Code
die Hooks eines Plugins selbst, wie bisher.

Grenzen:

- Claude Code startet die Hooks, die zu einem Aufruf passen, parallel. Ein
  Kit-Hook kann daher für einen Aufruf laufen, den die Regeln ablehnen, aber
  seine Antwort hebt keine Ablehnung auf.
- OpenCode-Plugins laufen im Prozess des Agenten und können alles, was er
  kann. Die zweite Prüfung fängt geänderte Argumente ab, nicht ein Plugin,
  das sie umgeht. Geben Sie nur Hooks frei, denen Sie vertrauen.

## Netzwerkregeln

Jeder Aufruf erhält einen eigenen Netzwerk-Namespace mit nftables,
**standardmäßig gesperrt**. Nur die Provider-Hosts des Agenten werden beim
Start aufgelöst, in `/etc/hosts` festgeschrieben und auf **TCP/443** erlaubt.
Standardmäßig gibt es in der Sandbox kein DNS.

| Agent | Standardziele für HTTPS |
|---|---|
| Codex | `api.openai.com`, `chatgpt.com`, `auth.openai.com` |
| Claude | `api.anthropic.com`, `console.anthropic.com`, `claude.ai`, `claude.com`, `platform.claude.com` |
| OpenCode | `opencode.ai` |

Sobald `api.anthropic.com` erlaubt ist, werden auch Anthropics veröffentlichte
Ingress-Netze `160.79.104.0/23` und `2607:6bc0::/48` auf TCP/443 freigegeben;
so bleibt die API bei DNS-Wechseln während langer Sitzungen erreichbar. OpenAI
veröffentlicht keine festen Netze; diese Ziele bleiben per DNS festgeschrieben.

| Option | Wirkung |
|---|---|
| `-p`, `--profile NAME` | Ein benanntes Bündel (mehrfach): `development` = Anbieter + PyPI + GitHub + APIs von OpenAI und Anthropic; `frontier` = `--frontier`; `dns`, `egress-all`, `offline` = die Optionen unten. Der Wizard nutzt dieselben Profile. |
| `-f`, `--frontier` | Zusätzlich alle OpenAI- und Anthropic-Ziele oben erlauben, für jeden Agenten. |
| `-y`, `--allow-pypi` | `pypi.org`, `files.pythonhosted.org` |
| `-g`, `--allow-github` | `github.com`, `api.github.com`, `codeload.github.com`, `raw.githubusercontent.com`, `objects.githubusercontent.com`, `release-assets.githubusercontent.com` |
| `-gl`, `--allow-gitlab` | `gitlab.com`, `registry.gitlab.com` |
| `-ak`, `--allow-kernel-org` | `kernel.org`, `www.kernel.org`, `git.kernel.org`, `cdn.kernel.org`, `mirrors.kernel.org`, `lore.kernel.org` |
| `-dd`, `--allow-duckduckgo` | `duckduckgo.com`, `html.duckduckgo.com`, `lite.duckduckgo.com`, `links.duckduckgo.com`, `api.duckduckgo.com` |
| `-A`, `--allow-host NAME` | Namen auflösen und festschreiben; TCP/443 erlauben. Groß- und Kleinschreibung zählen nicht (`Example.org` ist `example.org`). `*.DOMAIN` erlaubt jeden Namen unter DOMAIN (siehe unten). |
| `-a`, `--allow IP_ODER_CIDR` | Alle Protokolle und Ports zu diesem Ziel. |
| `-t`, `--allow-tcp IP:PORT` | Genau einen TCP-Port auf einer Adresse (IPv6: `[ADR]:PORT`). |
| `-P`, `--allow-host-port PORT` | Podman: `127.0.0.1:PORT` in der Sandbox erreicht denselben Port am Host-Loopback (mehrfach). |
| `-R`, `--dns` | Echtes DNS über `1.1.1.1` und `8.8.8.8` (nur UDP/TCP 53 zu diesen). |
| `-ns`, `--nameserver IP` | Stattdessen diesen Resolver nutzen (mehrfach; impliziert `--dns`). |
| `-E`, `--egress-all-allow` | Jedes ausgehende Ziel und jeden Port erlauben; impliziert `--dns`. |
| `-nd`, `--no-provider-defaults` | Standardziele des Agenten entfernen. |
| `-o`, `--offline` | Überhaupt kein externes Netz. |
| `-sn`, `--subnet CIDR` | Das eigene IPv4-Netz der Sandbox im Standardnetz (beide Backends): die Sandbox `.100`, Gateway `.2`; siehe unten. |
| `-s6`, `--subnet6 CIDR` | Podman: das eigene IPv6-`/64` der Sandbox (`::100`, Gateway `::2`). |
| `-nw`, `--network NAME`, `-ip`, `--ip`, `-i6`, `--ip6` | Podman: bestehendes privates Netz, statische Adressen. |

`--dns` löst Namen auf, öffnet aber nicht die aufgelösten Adressen; über
Anfragenamen können Daten hinausgelangen, daher ist das eine bewusste
Erweiterung. `--egress-all-allow` hält eingehenden Verkehr und Forwarding
geschlossen und das Host-Loopback verborgen, das lokale Netz ist aber
erreichbar, einschließlich Diensten, die der Host auf seinen LAN-Adressen
anbietet. `127.0.0.1` bezeichnet die Sandbox selbst, außer für Ports, die
mit `--allow-host-port` geöffnet sind. IP-Filterung kann Domains mit
gemeinsamen CDN-Adressen nicht unterscheiden. Podman nutzt `pasta`,
Bubblewrap `slirp4netns`; es werden keine Ports veröffentlicht.

**Host-Wildcards.** `--allow-host '*.example.com'` (beim Start oder während des Laufs
mit `sandbox-ai net allow`) erlaubt jeden Namen unter `example.com` (`api.example.com`,
`a.b.example.com`), nicht `example.com` selbst: beides angeben, wenn beides gemeint
ist. Die Namen sind vorher nicht bekannt, also wird beim Start keiner
festgeschrieben: Schlägt der Agent einen nach, hält der DNS-Stub die Anfrage und
stellt ihre Frage, der Launcher beantwortet sie sofort auf dem Host (dort
aufgelöst, nur öffentliche Adressen, innerhalb von `allow_hosts` der Policy), und der
Stub gibt die Adressen zurück, während der Client noch wartet; von da an ist der
Name für den Lauf festgeschrieben und offen, wie eine freigegebene Namensauflösung.
Eine Wildcard braucht zwei Labels darunter (`*.com` wird abgelehnt) und eine
Sandbox, die fragt (nicht `--ask off`); `net revoke` nimmt eine Wildcard zurück,
die darunter geöffneten Namen bleiben für den Lauf offen.

**Dienste auf dem eigenen Host** (ein lokaler Git-Server, eine Datenbank) sind
mit `--allow-tcp` nicht erreichbar: `pasta` gibt der Sandbox die eigene
IP-Adresse des Hosts, diese Adresse führt also zurück in die Sandbox. Das
vermeidet Kollisionen mit anderen Rechnern im Netz, verbirgt aber den Host.
Stattdessen `--allow-host-port PORT` nutzen: pasta leitet dann
`127.0.0.1:PORT` in der Sandbox auf denselben Port am
Loopback des Hosts weiter, sonst wird nichts auf dem Host erreichbar. Ein
Git-Remote wie `http://localhost:3080/...` funktioniert in der Sandbox
unverändert. Host ist der Rechner, auf dem Podman läuft, mit `--podman-url`
also dieser. Unterstützt nur vom Podman-Backend mit seinem Standardnetz;
unvereinbar mit `--offline`, `--network` und `--backend bwrap`.

```bash
./sandbox-ai claude --workspace "$HOME/src/demo" --allow-host-port 3080
```

**Die eigenen Adressen der Sandbox** (`--subnet`, `--subnet6`). Ohne sie gibt
Podmans `pasta` der Sandbox die eigenen Adressen des Hosts, und `slirp4netns`
(Bubblewrap) nutzt `10.0.2.0/24` (die Sandbox `10.0.2.100`, Gateway
`10.0.2.2`, DNS-Weiterleitung `10.0.2.3`) und `fd00::/64`. `-sn`, `--subnet
CIDR` gibt beiden Backends ein anderes IPv4-Netz, etwa wenn ein VPN oder LAN
`10.0.2.0/24` nutzt; beide legen es an wie slirp4netns: die Sandbox auf `.100`,
das Gateway auf `.2`, die DNS-Weiterleitung auf `.3` (Podman: `pasta -a .100
-n PRÄFIX -g .2 --dns-forward .3`; Bubblewrap: `slirp4netns --cidr`).
`-s6`, `--subnet6 CIDR` tut dasselbe für IPv6 mit Podman, ein `/64` in
`fc00::/7` (die Sandbox `::100`, Gateway `::2`); slirp4netns hat für sein
IPv6-Netz keine Option und nutzt immer `fd00::/64`, darum lehnt Bubblewrap es ab.

```bash
./sandbox-ai claude --subnet 10.20.0.0/24 --subnet6 fd20::/64
```

Abgelehnt werden: Netze außerhalb der privaten Bereiche (`10.0.0.0/8`,
`172.16.0.0/12`, `192.168.0.0/16`, `100.64.0.0/10`, `fc00::/7`), also auch
Loopback, Link-local und Multicast; eine Adresse statt des Netzes
(`10.20.0.100/24`); IPv4-Präfixe außerhalb von `/8` bis `/25` (die Sandbox
bekommt die 100. Adresse) und IPv6-Präfixe außer `/64`; ein Netz, das sich mit
einem überschneidet, in das dieser Host routet (`/proc/net/route`,
`/proc/net/ipv6_route`; mit `--podman-url` nicht geprüft, dessen Routen sind
die eines anderen Rechners); und ein Netz, das ein Ziel enthält, das der Lauf
erlaubt (`--allow`, `--allow-tcp`, ein Resolver, die Adresse eines
festgeschriebenen Hosts): Eine solche Adresse erreichte statt des Ziels die
eigene Leitung, das Gateway oder die DNS-Weiterleitung der Sandbox. Die eine
Ausnahme ist `--nameserver` auf `.3`, die Weiterleitung des Backends zu den
Resolvern des Hosts. Gemerkte Freigaben innerhalb des Netzes werden mit einem
Hinweis übergangen, und `sandbox-ai net allow` lehnt Ziele darin ab.
`--subnet` ist unvereinbar mit `--offline`, und mit `--network NAME`
entscheidet das Podman-Netz (dort gelten `--ip`/`--ip6`).

Sonst hängt nichts an der Adresse der Sandbox: Firewall, Verbindungs-Proxy (er
leitet auf Loopback um), `/etc/resolv.conf` und `--allow-host-port` bleiben
gleich. Mit `--subnet` teilt die Podman-Sandbox die Adresse des Hosts nicht
mehr; die LAN-Adresse des Hosts führt dann wie bei Bubblewrap zum Host, nicht
zurück in die Sandbox, und ist nur erreichbar, wo die Optionen es erlauben.
Der Plan von `--dry-run` zeigt die Adressen als `sandbox_network`, und der
Start nennt ein gewähltes Netz.

**Deine Voreinstellungen**: `~/.config/sandbox-ai/defaults.json` setzt das
Netz für jeden Start, der keines wählt (Optionen und Projektdateien gehen vor):

```json
{"version": 1, "subnet": "10.20.0.0/24", "subnet6": "fd20::/64", "keep_days": 90}
```

`keep_days` (optional) lässt aufbewahrte Starts verfallen: Jeder neue Start
entfernt die gestoppten, die länger als so viele Tage unbenutzt sind, und
`sandbox-ai gc` nimmt den Wert, wenn kein `--older-than` angegeben ist. Ohne
ihn bleiben aufbewahrte Starts, bis Sie sie entfernen
([jeder Start](#jeder-start-namen-lösen-aufbewahren-fortsetzen)).

Sie wird nur gelesen, wenn sie außerhalb aller Workspaces, rw-Mounts und
`--state-dir` liegt, und gilt nicht mit `--offline` oder `--network`; unter
Bubblewrap bleibt ihr `subnet6` weg. Die Richtlinie des Administrators kann
ebenfalls eine Voreinstellung und eine Obergrenze setzen
([Richtlinie des Administrators](#richtlinie-des-administrators)). Der Schlüssel
`"mode"` von `defaults.json` wählt den [Sicherheitsmodus](#sicherheitsmodi) jedes Starts, der keinen
wählt.

**Pushen nach GitHub**: `--git-token-env https://github.com=NAME` gibt den
Token aus der Host-Variable `NAME` an Git, nur für `https://github.com`, über
einen Credential-Helper, der per `GIT_CONFIG_*`-Variablen gesetzt wird,
zusätzlich als `GH_TOKEN` für die `gh`-CLI, und impliziert `--allow-github`.
Im Sandbox-HOME wird nichts geschrieben, und Helper aus einer dortigen
`~/.gitconfig` werden für GitHub ignoriert. `--git-identity` committet als
`user.name` und `user.email` des Hosts.

Der Agent kann den Token lesen und ihn über GitHub selbst weitergeben, etwa in
einem Gist. Daher einen **Fine-grained Token** nutzen, beschränkt auf die
nötigen Repositories, nur mit `Contents: Read and write` und kurzer
Laufzeit, und wichtige Branches auf GitHub schützen. Ein Push-Proxy auf dem
Host, der den Token aus der Sandbox heraushält, ist geplant, siehe
[TODO.md](TODO.md).

```bash
export SANDBOX_GH_TOKEN=github_pat_...   # fine-grained, nur ausgewählte Repositories
./sandbox-ai claude --git-token-env https://github.com=SANDBOX_GH_TOKEN --git-identity
```

**Pushen auf einen eigenen Git-Server** (Gitea, Forgejo, GitLab, ...):
`--git-token-env URL=NAME` gibt den Token aus der Host-Variable `NAME` an
Git, nur für genau diese `URL` (Schema, Host und Port, ohne Pfad; mehrfach
möglich). Den Benutzer als `http://USER@HOST:PORT` angeben; ohne ihn sendet
Git `x-access-token`, was Gitea, Forgejo und GitLab mit einem Token
akzeptieren. Eine URL auf dem Loopback des Hosts (`localhost`, `127.0.0.1`,
`[::1]`) impliziert `--allow-host-port PORT` und braucht das
Podman-Backend mit seinem Standardnetz; nur dort ist reines `http` erlaubt.
Andere Hosts brauchen `https`; die Option impliziert `--allow-host HOST`
(Name gepinnt, TCP/443), ein anderer Port braucht zusätzlich
`--allow-tcp IP:PORT`. Die URL muss
genau zum Host des Remotes passen: Ein Token für `http://localhost:3080`
antwortet nicht für `http://127.0.0.1:3080`. Wie bei GitHub werden Helper
aus einer `~/.gitconfig` in der Sandbox für diese URL ignoriert, und der
Agent kann den Token lesen (als `SANDBOX_GIT_TOKEN_0`, ...); daher einen
auf die nötigen Repositories beschränkten Token anlegen.

```bash
export SANDBOX_GITEA_TOKEN=...   # Gitea: Einstellungen -> Anwendungen, Scope repository
./sandbox-ai claude --git-token-env http://alice@git.example.internal:3000=SANDBOX_GITEA_TOKEN --git-identity
```

Statt einer Variable liest `--git-token-file URL=DATEI` den Token aus einer Datei, die Ihnen gehört, Modus
`0600` hat, kein Symlink ist und außerhalb aller Workspaces liegt.
`sandbox-ai wizard` legt solche Dateien unter `~/.config/sandbox-ai/tokens/` an,
wenn Sie dort einen Token eingeben (verdeckte Eingabe); die
Projektkonfiguration nennt dann nur die Datei, nie den Token.

**Tokens, die der Agent nie sieht**: Mit `--token-proxy` (`-tp`) bleiben die
Tokens von `--git-token-env`, `--git-token-file` und `--git-token-secret`
außerhalb der Sandbox. Sie gehen nur an den Verbindungs-Proxy, dessen
Verzeichnis der Agent nicht lesen kann, und der Proxy setzt sie in Gits eigene
Anfragen (`info/refs`, `git-upload-pack`, `git-receive-pack`) an genau den
HTTPS-Server ein, dessen Zertifikat er geprüft hat. Der Agent klont und pusht
wie gewohnt, durch die Datenschutzprüfung, kann aber keinen Token lesen, ihn
nicht für die API des Servers nutzen und nirgendwohin schicken. Im
Verbindungs-Log erscheint der Token nie. Server mit reinem `http` (ein Gitea am
Loopback des Hosts) und `--git-credentials` gehen auf diese Weise nicht; `gh`
bekommt dann kein `GH_TOKEN`.

```bash
sandbox-ai claude --git-token-secret https://github.com=GH_PUSH --token-proxy --git-identity
```

**Vorhandene Git-Logins**: `--git-credentials DATEI` übernimmt die Logins aus
einer Datei im Format von Gits `store`-Credential-Helper, meist
`~/.git-credentials` (eine `https://USER:PASSWORT@HOST[:PORT]` pro Zeile).
Jedes Login antwortet für seinen Server genau wie eine `--git-token-*`-Option,
mit denselben Netzwerkregeln: Ein Login für `http://localhost:3080`
impliziert `--allow-host-port 3080`, eines für `https://github.com`
`--allow-github` und `GH_TOKEN`, andere `--allow-host HOST`. Die Datei muss
Ihnen gehören, Modus `0600` haben, kein Symlink sein und außerhalb aller
Workspaces liegen; sie wird auf dem Host gelesen und nie gemountet. Wie in
Git gewinnt die erste Zeile für einen Server, und ein Pfad in einer Zeile
schränkt sie nicht ein; eine `--git-token-*`-Option für denselben Server
gewinnt über die Datei. Zeilen, die die Sandbox nicht nutzen kann (reines
`http` zu einem anderen Host, ein Loopback-Server ohne das Podman-Standardnetz,
fehlerhafte Zeilen), werden mit einem Hinweis übersprungen, der die
Zeilennummer nennt, nie den Inhalt; eine Datei ohne nutzbares Login wird
abgelehnt. **Der Agent kann jedes übergebene Login lesen**
(`SANDBOX_GIT_USER_0`/`SANDBOX_GIT_TOKEN_0`, ...), daher in dieser Datei nur
Tokens halten, keine Kontopasswörter, oder eine eigene Datei nur mit den
Logins anlegen, die die Sandbox braucht.

```bash
./sandbox-ai claude --git-credentials ~/.git-credentials --git-identity
```

### Git-Logins im Proxy (sandbox-ai git-logins)

Die Logins Ihrer Git-Server können außerhalb jeder Sandbox liegen: Jeder Server
wird einmal eingetragen, und der Verbindungs-Proxy jeder Sandbox fügt sein Token
auf dem Weg in Gits Smart-HTTP-Anfragen an diesen Server ein (clone, fetch,
push), und nirgends sonst. Das Git des Agenten braucht keine Zugangsdaten, kein
Token kommt je in seine Sandbox; die Privatsphäre-Prüfung von Pushes kennt die
Tokens.

```bash
sandbox-ai secrets set GITEA_TOKEN                        # das Token, von der Standardeingabe
sandbox-ai git-logins add http://localhost:3080 --secret GITEA_TOKEN --user sie
sandbox-ai secrets set GH_PUSH
sandbox-ai git-logins add https://github.com --secret GH_PUSH
sandbox-ai git-logins list | check | remove URL
```

Die Liste (`~/.config/sandbox-ai/git-logins.json`, Modus `0600`) enthält Server,
Benutzer und Namen von Geheimnissen, nie ein Token. Plain-HTTP gibt es nur für
den Loopback dieses Hosts (ein lokales Gitea): Der Proxy fügt dieses Token nur
für den Loopback-Port ein, den die Sandbox als den des Hosts erreicht
(`--allow-host-port 3080` oder während des Laufs `sandbox-ai net allow
--allow-host-port 3080`); ein HTTPS-Login geht nur an den Server, dessen
Zertifikat der Proxy geprüft hat. Die Logins öffnen die Server nicht: Sie müssen
wie sonst erreichbar sein (`--allow-github`, `--allow-host`). Eine
`--git-token-*`-Option für denselben Server gewinnt für ihren Start;
`--git-logins off` startet ohne sie; Läufe ohne Verbindungs-Proxy und die
Sandboxen von MCP-Servern bekommen sie nie. Mit `--token-proxy` kommen die
`--git-token-*`-Tokens dazu, auch das des lokalen Gitea.

### Veröffentlichen in Registries und Forges (sandbox-ai publish-logins, --publish)

npm, PyPI, TestPyPI, crates.io, GitLab und GitHub gehen genauso: Das Token
bleibt bei Ihnen, der Proxy fügt es erst auf der Leitung ein. Anders als
Git-Logins nutzt ein Start sie nur, wenn er es sagt, denn ein veröffentlichtes
Paket lässt sich nicht zurückholen:

```bash
sandbox-ai secrets set PYPI_TOKEN
sandbox-ai publish-logins add pypi --secret PYPI_TOKEN
sandbox-ai publish-logins add gitlab --secret GL_TOKEN --url https://git.example.org   # Ihr eigener Server
sandbox-ai publish-logins kinds                       # was jede Art öffnet, setzt und erfragt
sandbox-ai claude --publish pypi,npm                  # dieser Start darf dort veröffentlichen
```

Mit `--publish ART[,ART]` erreicht die Sandbox die Upload- und API-Hosts dieser
Art. Die Werkzeuge des Agenten bekommen ihre üblichen Variablen, die nur einen
Platzhalter enthalten: `TWINE_USERNAME`/`TWINE_PASSWORD`, `UV_PUBLISH_TOKEN`,
`POETRY_PYPI_TOKEN_PYPI`, `HATCH_INDEX_AUTH`, `FLIT_PASSWORD` (PyPI),
`NPM_TOKEN` mit einer npm-Konfiguration, die es nutzt, `CARGO_REGISTRY_TOKEN`,
`GITLAB_TOKEN` und `GH_TOKEN`. Der Proxy setzt das Token an ihre Stelle, nur in
einen Anmelde-Header von HTTPS-Anfragen an die Hosts dieser Art, auch in das
Basic-Login von PyPI. Das Regelwerk `publish` fragt vor jeder Anfrage, die
veröffentlicht oder etwas zurücknimmt: ein Upload, ein npm publish, dist-tag
oder unpublish, ein Crate publish, yank oder Besitzerwechsel, ein GitHub- oder
GitLab-Release, ein Paket und eine über die API geschriebene Datei oder ein
Commit, den die Datenschutzprüfung eines Pushs nicht sähe. Mit `--ask off`
werden diese Anfragen abgewiesen. `--publish github` zusammen mit einer
`--git-token-*`-Option für github.com braucht `--token-proxy`, da diese Option
dem Agenten `GH_TOKEN` selbst gäbe. Die Liste
(`~/.config/sandbox-ai/publish-logins.json`, Modus `0600`) enthält Arten, Hosts
und Namen von Geheimnissen, nie ein Token.

### Identitäten (sandbox-ai identities, --identity)

Eine Identität ist, als wer eine Sandbox arbeitet: ein Commit-Name und eine
Adresse, Git-Logins und Publish-Logins unter einem Namen. Sie wählen sie pro
Start, in einem Preset oder in der `.sandbox-ai.json` des Projekts
(`"options": ["--identity", "c0decave"]`):

```bash
sandbox-ai identities add c0decave --name dash --email c0decave@users.noreply.github.com
sandbox-ai identities login c0decave https://github.com --secret GH_C0DECAVE --user c0decave
sandbox-ai identities publish c0decave pypi --secret PYPI_C0DECAVE
sandbox-ai identities add investigation --description "OSINT, keine Konten"
sandbox-ai identities list | show NAME | check | logout NAME URL | unpublish NAME ART | remove NAME
sandbox-ai claude --identity c0decave --publish pypi
```

Ohne `--identity` arbeitet ein Start als `default`: mit den Logins von
`sandbox-ai git-logins` und `publish-logins` und mit `--git-identity` mit
`user.name` und `user.email` Ihres Hosts. Mit `--identity NAME` arbeitet er
nur als NAME: Commit-Name und Adresse von NAME (als `GIT_CONFIG_*`, daher wird
`--git-identity` daneben abgelehnt), die Git-Logins von NAME statt die von
default und mit `--publish` die Publish-Logins von NAME. Eine Identität ohne
Logins hat gar keine, sodass eine Sandbox für eine Recherche keines Ihrer
Konten trägt. Zwei Konten auf einem Server sind zwei Identitäten. Die Tokens
bleiben in `sandbox-ai secrets` und erreichen nur den Verbindungs-Proxy; die
Liste (`~/.config/sandbox-ai/identities.json`, Modus `0600`) enthält Namen,
Adressen, Server und Namen von Geheimnissen. Eine URL mit Passwort wird überall
abgelehnt, wo ein Git-Server genannt wird.

**Antworten.** Hat der Proxy eine Anmeldung eingefügt (ein Git-Login, einen
Schlüssel von `--llm` oder `--api-key`, ein Publish-Token), bittet er den
Server um eine unkomprimierte Antwort und maskiert die Anmeldung darin, in
Headern und Body (auch chunked), mit Sternen gleicher Länge. Ein Server, der
trotzdem komprimiert, bekommt seine Antwort unverändert weitergereicht, und das
Log vermerkt sie als `unscrubbed`.

### Netzwerk einer laufenden Sandbox ändern

Die Netzwerkoptionen gelten ab dem Start, aber einer laufenden Sandbox lässt
sich ohne Neustart mehr (oder weniger) erlauben:

```bash
sandbox-ai net list                                    # laufende Sandboxes
sandbox-ai net allow --allow-host pypi.org --allow-github
sandbox-ai net allow claude-1a2b3c4d --allow-tcp 192.0.2.10:5432
sandbox-ai net show                                    # was während des Laufs dazukam
sandbox-ai net revoke --allow-host pypi.org
sandbox-ai net allow --dns                             # echte DNS-Abfragen (oder --nameserver IP)
sandbox-ai net allow --git-token-env http://localhost:3080=GITEA_TOKEN   # ein Git-Login
sandbox-ai net allow --allow-host-port 3080                               # 127.0.0.1:3080 des Hosts
sandbox-ai stop claude-1a2b3c4d                        # beenden; ihr Launcher räumt auf
sandbox-ai net untaint claude-1a2b3c4d                 # den Taint des Laufs zurücknehmen (siehe Regelwerke)
sandbox-ai net allow --allow-host api.osv.dev --keep   # und auch für die nächsten Starts (.sandbox-ai.json)
```

`--keep` schreibt die Optionen zusätzlich in die `.sandbox-ai.json` des
Projekts (im Haupt-Workspace des Laufs) und vertraut der neuen Datei sofort,
sodass der nächste Start sie hat. Das geschieht nur, solange diese Datei genau
die ist, der Sie vertraut haben (der Agent kann den Workspace beschreiben, und
seine Änderung darf nie mit-vertraut werden), oder es entsteht eine neue für
den Agenten des Laufs; geht das nicht, ändert sich nichts, auch nicht der Lauf.
Ein Git-Login wird als Verweis behalten (`--git-token-env URL=VAR`), nie als
Token.

`sandbox-ai net show [LAUF] --keep` behält auf dieselbe Weise, was der Lauf bis
dahin bekommen hat: was Sie mit `net allow` gegeben haben, Wildcards, Netze,
DNS, Host-Ports, Geräte und hinzugefügte Workspaces sowie was „immer“ erlaubt
wurde. Was eine Frage nur einmal oder für den Lauf geöffnet hat, bleibt draußen
(und wird genannt); `--keep-all` behält auch das. Beide zeigen jede Option, bevor
die Datei geschrieben wird, und beide werden abgelehnt, solange die Richtlinie
des Administrators Antworten unter „immer“ begrenzt.

Den Namen eines Laufs zeigt `list`; der Agent bekommt ihn als
`$SANDBOX_AI_RUN` und kann so den genauen Befehl nennen. Er darf fehlen, wenn
nur eine Sandbox läuft (oder nur eine für das aktuelle Verzeichnis).
Namen aus `--allow-host` werden auf dem Host aufgelöst und in `/etc/hosts`
der Sandbox eingetragen; der Supervisor trägt die Adressen in Firewall und
Proxy ein. Änderungen gelten bis zum Ende der Sandbox; der Befehl nennt die
Optionen, mit denen sie dauerhaft werden. Nur Sie können das: Der
Steuer-Socket liegt in `$XDG_RUNTIME_DIR/sandbox-ai/runs/LAUF` (Modus `0700`)
und ist für den Agenten unsichtbar. Bereits offene Verbindungen bleiben nach
`revoke` offen. `--dns` (oder `--nameserver IP`, wiederholbar) gibt während
des Laufs echtes DNS: Der DNS-Stub der Sandbox leitet jeden nicht gepinnten
Namen an diese Nameserver weiter und protokolliert jede Abfrage (`dns.jsonl` im
Log-Verzeichnis); geöffnet wird nur UDP und TCP 53 zu ihnen, die Adressen, zu
denen die Namen auflösen, bleiben zu, solange Sie sie nicht auch freigeben. Ein
Name mit einem Köder-Token wird nie weitergeleitet. Das braucht den Stub, den
jeder Lauf mit Verbindungs-Proxy oder `--log-dir` hat.

`--allow-host-port PORT` (wiederholbar) lässt die laufende Sandbox
`127.0.0.1:PORT` des Hosts erreichen wie die Start-Option, über ein Relay: pasta
kann einer laufenden Sandbox keine Weiterleitung hinzufügen. Der Befehl startet
auf dem Host einen kleinen Prozess, der auf einem Unix-Socket im Verzeichnis
des Laufs lauscht (Modus `0700`, für den Agenten unsichtbar) und jede
Verbindung zu `127.0.0.1:PORT` des Hosts aufbaut, nie woandershin; in der
Sandbox lauscht der Supervisor auf `127.0.0.1:PORT` und `[::1]:PORT` und
verbindet die Verbindungen des Agenten damit. Sie laufen zuerst durch den
Verbindungs-Proxy wie die beim Start weitergeleiteten Ports (ein Push zu einem
lokalen Git-Server trifft also die Privatsphäre-Prüfung). Nur Sie können ein
Relay starten (der Agent erreicht weder den Steuer-Socket noch das Verzeichnis
des Laufs); höchstens 16 Ports und 64 Verbindungen je Port gleichzeitig;
`revoke --allow-host-port PORT` beendet es, und es endet von selbst mit dem
Lauf. Ein seit dem Start weitergeleiteter Port oder einer, auf dem in der
Sandbox schon etwas lauscht, wird abgelehnt. Das geht bei Läufen ab sandbox-ai
0.1.37; die Policy des Administrators und die Sicherheitsmodi `high` und
`paranoid` lehnen es ab wie die Start-Option.

`--git-token-env URL=NAME`, `--git-token-file URL=DATEI` und
`--git-token-secret URL=SECRET` geben der laufenden Sandbox einen Git-Login wie
beim Start: Das Token wird auf dem Host gelesen (Ihre Variable, eine Datei von
Ihnen mit Modus `0600` oder `sandbox-ai secrets`) und in das Session-Verzeichnis
des Laufs geschrieben, das die Sandbox nur lesen kann und das am Ende des Laufs
gelöscht wird. Ein Git-Helper, den jeder Start einrichtet (vor den Logins der
Start-Optionen, die für ihren Server gewinnen), gibt es Git; `show` nennt Server
und Benutzer, nie das Token, und `revoke --git-token-env URL=BELIEBIG` nimmt
einen Login zurück. Der Server wird wie beim Start geöffnet: `https://github.com`
das GitHub-Preset, ein anderer HTTPS-Server sein Name; einen Server auf dem
Loopback des Hosts (`http://localhost:3080`) erreicht die Sandbox, wenn der
Lauf seinen Port weiterleitet (`--allow-host-port` oder ein Login dafür beim
Start) oder ihn per Relay erreicht (`net allow --allow-host-port PORT`, unten). Das geht bei Läufen ab sandbox-ai 0.1.37 und nicht mit `--token-proxy`
(dort bleiben die Tokens außerhalb der Sandbox); `gh` sieht einen so
hinzugefügten Login nicht (`GH_TOKEN` wird nur beim Start gesetzt), und die
Privatsphäre-Prüfung von Pushes kennt sein Token nur an seiner Form. Ein Lauf im Sicherheitsmodus `high` oder `paranoid` bekommt
nichts, was sein Modus ablehnt ([Sicherheitsmodi](#sicherheitsmodi)).

`sandbox-ai stop NAME` beendet eine Sandbox aus einem anderen Terminal, welcher
Art der Start auch ist (ein Lauf, eine Session, ein MCP-Server). Bei einem Lauf
schickt es dem Launcher der Sandbox SIGTERM, der wie nach einem normalen Ende
aufräumt, nachdem es geprüft hat, dass die PID noch dieser Launcher ist (Ihr
Prozess, mit der festgehaltenen Startzeit, mit `agent_sandbox.py`), und wartet
bis zu `--wait SEKUNDEN` (Standard 30) auf sein Ende. `sandbox-ai net stop LAUF`
ist die ältere Form davon für Läufe. `list` findet auch Sandboxes, die mit einem
anderen `XDG_RUNTIME_DIR` (oder ohne, etwa aus cron oder `su`) gestartet
wurden: Es liest auch `/run/user/UID/sandbox-ai/runs` und
`~/.local/state/sandbox-ai/runs`.

## VPN

Vor eine Sandbox lässt sich ein VPN setzen (Podman-Backend): Der Tunnel läuft
in einem eigenen Container im Netzwerk-Namespace des Pods, ein **Kill Switch**
lässt nichts anderes hinaus (IPv4 und IPv6), und bevor der Agent startet,
werden die von außen gesehenen Adressen und das DNS **geprüft**. Firewall,
Verbindungs-Proxy und Rückfragen von sandbox-ai arbeiten wie immer; ihre
Verbindungen gehen durch den Tunnel.

```bash
sandbox-ai build vpn                                        # einmal: localhost/sandbox-ai-vpn
sandbox-ai vpn add home --wireguard ~/Downloads/wg-home.conf  # jede wg-quick-Datei: eigener Server, Proton, IVPN, AirVPN ...
sandbox-ai vpn add mv --mullvad --location se-sto           # fragt nach der Kontonummer (nicht angezeigt, nicht gespeichert)
sandbox-ai vpn add office --openvpn office.ovpn --auth-secret OFFICE_LOGIN
sandbox-ai vpn add ts --tailscale --auth-key-secret TS_KEY --exit-node exit-1
sandbox-ai vpn add anon --tor --socks --exit-country de     # Tor; --bridges DATEI für obfs4-Bridges
sandbox-ai vpn list                                         # auch: show NAME, remove NAME, --json
sandbox-ai vpn check mv                                     # allein aufbauen, prüfen, abbauen
sandbox-ai claude --vpn mv                                  # oder "options": ["--vpn", "mv"] in .sandbox-ai.json
```

| Art | Profil aus | Tunnel |
|---|---|---|
| WireGuard | `--wireguard DATEI`: jede wg-quick-Konfiguration: mehrere Peers, Endpunkte als Namen oder IPv4-/IPv6-Adressen, Teilmengen in `AllowedIPs` (Split-Tunnel), `PresharedKey`, `PersistentKeepalive`, `MTU`, `DNS`. Abgelehnt: `PreUp`/`PostUp`/`PreDown`/`PostDown` (wg-quick führt sie als root aus), `Table`, `SaveConfig = true` | `wg0`: das WireGuard des Kernels, wenn der Host das Modul geladen hat, sonst wireguard-go über `/dev/net/tun` |
| Mullvad | `--mullvad [--account-secret GEHEIMNIS] [--location se\|se-sto\|RELAY] [--no-ipv6] [--endpoint-ipv6]`: Ein neuer WireGuard-Schlüssel wird über Mullvads API registriert (ein Gerät Ihres Kontos), ein Relay aus der Liste gewählt. Die Kontonummer kommt aus `sandbox-ai secrets` oder wird eingegeben, dient nur dieser einen Anfrage und wird nie geschrieben oder ausgegeben | wie WireGuard, DNS `10.64.0.1`; die Prüfung fragt `am.i.mullvad.net`, das einen Mullvad-Ausgang bestätigt |
| Proton VPN | Proton hat keine einfache Schlüssel-API (die Anmeldung ist SRP): Laden Sie im Proton-Konto eine WireGuard-Konfiguration herunter und fügen Sie sie mit `--wireguard` hinzu | wie WireGuard |
| OpenVPN | `--openvpn DATEI [--auth-secret GEHEIMNIS] [--split]`: eine Client-`.ovpn`. Dateien, die sie daneben nennt (`ca`, `cert`, `key`, `tls-auth`, `tls-crypt` ...), werden übernommen; Benutzername und Passwort (`auth-user-pass DATEI` daneben oder inline) wandern als `USER:PASSWORT` nach `sandbox-ai secrets`. Abgelehnt: alles, was Programme startet (`up`, `down`, `script-security`, `plugin` ...), andere Dateien liest oder schreibt (`config`, `log`, `management`, `askpass` ...), Proxys, tap-Geräte, `<connection>`-Blöcke und unbekannte Direktiven | `tun0`, voller Tunnel, außer mit `--split` (dann die Routen des Servers; der Rest bleibt gesperrt) |
| Tailscale, Headscale | `--tailscale [--auth-key-secret GEHEIMNIS] [--exit-node HOST] [--login-server URL] [--ephemeral] [--tailnet-allow CIDR]` | `tailscale0`: tailscaled im tun-Modus |
| Tor | `--tor [--bridges DATEI \| --bridge-secret GEHEIMNIS] [--exit-country CC[,CC]] [--socks]`: Bridges sind obfs4- oder einfache Zeilen, wie bridges.torproject.org sie ausgibt, aus einer `0600`-Datei (in `sandbox-ai secrets` verwahrt) oder einem eigenen Geheimnis | keine Schnittstelle: Tors TransPort nimmt jede TCP-Verbindung, sein DNSPort jede Abfrage ([Tor](#tor)) |

**Ablauf.** Der Starter legt den Pod an, der VPN-Container tritt ihm bei:
`localhost/sandbox-ai-vpn` (WireGuard-Werkzeuge, wireguard-go, OpenVPN,
Tailscale, Tor, nftables), schreibgeschützt, ohne Capability außer `NET_ADMIN`
(und `NET_RAW` für tailscaled; für Tor `SETUID`, `SETGID` und
`NET_BIND_SERVICE` statt `/dev/net/tun`: tor bindet Port 53 und wird dann
`debian-tor`), mit `/dev/net/tun` und nichts vom Host außer seinem
Laufzeit-Skript (und dem Knotenzustand von Tailscale). Profil und Schlüssel
kommen über seine Standardeingabe. Er löst die Endpunkte einmal auf, lädt den
Kill Switch, baut den Tunnel auf, führt die Prüfungen aus und löst die
erlaubten Hostnamen des Agenten über den Resolver des VPN auf; dann startet der
Container des Agenten im selben Pod. Der Tunnel bleibt, bis der Agent endet.

**Kill Switch** (nftables-Tabelle `sandbox_ai_vpn` im Namespace, für IPv4 und
IPv6 gleich): Pakete gehen nur über `lo`, die Schnittstelle des Tunnels und als
eigene Pakete des Tunnels hinaus, die seine Markierung tragen (fwmark von
WireGuard und `--mark` von OpenVPN `0xca6c`; bei tailscaled `0x80000`), zu den
beim Start aufgelösten Adressen, Ports und Protokollen der Endpunkte. Eine
Markierung zu setzen braucht `CAP_NET_ADMIN`, das weder Agent noch Proxy haben;
selbst an den Endpunkt zu senden bringt sie also nicht weiter, und der Name des
Endpunkts wird nicht erneut aufgelöst. Eine Verbindung wird nicht wegen ihres
Zustands durchgelassen: Was durch den Tunnel ging, läuft nicht über den Uplink
weiter, wenn der Tunnel wegfällt. Eine Adressfamilie, für die der Tunnel keine
Adresse hat (ein reines IPv4-VPN), wird gesperrt, nicht durchgelassen; ein
Split-Tunnel leitet nur seine Netze, der Rest wird abgewiesen. Stirbt der
Tunnel, bleibt der Kill Switch (der Lauf meldet es); bis die Sandbox endet,
geht nichts hinaus. Tailscale mit Exit-Node lässt den markierten Verkehr von
tailscaled (Koordinationsserver, DERP-Relays, Peers) überallhin, alles andere
nur über `tailscale0`.

**Prüfungen** vor dem Start des Agenten (und mit `sandbox-ai vpn check NAME`),
für IPv4 und IPv6 getrennt, aus dem Namespace heraus:

- Die Adresse, die die Echo-Dienste sehen (Standard `api64.ipify.org` und
  `ifconfig.co`; bei Mullvad zuerst `am.i.mullvad.net`), muss sich von der
  eigenen öffentlichen Adresse dieses Hosts unterscheiden, die zugleich vom Host
  aus erfragt wird; Mullvad muss seinen Ausgang bestätigen; eine Adressfamilie,
  die der Tunnel nicht trägt, muss unerreichbar bleiben.
- Jeder Resolver des VPN muss über die Schnittstelle des Tunnels erreicht werden
  und antworten; `whoami.akamai.net`, das mit der Adresse des fragenden
  Resolvers antwortet, darf nicht die eigene Adresse dieses Hosts zeigen (ein
  DNS-Leck).

Eine fehlgeschlagene Prüfung stoppt den Start mit den Gründen; `--vpn-check
warn` warnt nur. Die Ergebnisse gehen ins Terminal und nach `vpn.jsonl` im
Log-Verzeichnis des Laufs (ohne Schlüssel). Die Echo-Dienste lassen sich je
Profil wählen (`vpn add ... --check-url URL`, `--no-dns-probe`) oder für alle
Profile in `~/.config/sandbox-ai/vpn/checks.json` (`{"urls": [...],
"dns_probe": "whoami.akamai.net", "timeout": 8}`). Ein VPN, das dort endet, wo
dieser Host steht (der eigene Server daheim, von daheim genutzt), fällt durch
die Adressprüfung; nutzen Sie dort `--vpn-check warn`.

**DNS.** Ohne `--dns` bleiben die erlaubten Namen wie immer gepinnt, beim Start
über das VPN aufgelöst. Mit `--dns` sind die Resolver der Sandbox die des VPN:
der `DNS`-Eintrag der Konfiguration, Mullvads `10.64.0.1`, was der
OpenVPN-Server vorgibt, Tailscales `100.100.100.100`; erreicht durch den
Tunnel. `--nameserver` verträgt sich nicht mit `--vpn`; andere Resolver kommen
ins Profil (`vpn add NAME ... --dns IP --force`).

**Tailscale.** Mit `--exit-node` geht jede Verbindung über den Exit-Node. Ohne
geht nur das Tailnet über Tailscale: Der Agent darf `100.64.0.0/10` und
`fd7a:115c:a1e0::/48` erreichen (oder was `--tailnet-allow` nennt; die ACLs des
Tailnets gelten zusätzlich), alles andere folgt den eigenen Regeln der Sandbox
über das normale Netz, und es gibt keine Adressprüfung. Der Zustand des Knotens
liegt je Profil in `~/.local/share/sandbox-ai/vpn/NAME` (nur der VPN-Container
bindet ihn ein), daher läuft ein Profil jeweils einmal; `--ephemeral` (mit
Auth-Key) gibt jedem Start einen eigenen Knoten. Ohne Auth-Key zeigt der erste
Start eine Anmelde-URL. `--login-server` nutzt einen Headscale-Server.

<a id="tor"></a>**Tor.** tor läuft im VPN-Container als Client (mit obfs4proxy und den
GeoIP-Dateien). Eine Tunnel-Schnittstelle gibt es nicht: jede TCP-Verbindung
des Namespace wird auf Tors TransPort umgeleitet (`127.0.0.1:9040`; die des
Agenten erreichen wie immer zuerst den Verbindungs-Proxy, Freigabeliste,
Rückfragen, TLS-Prüfung und Verbindungsprotokoll gelten also), jede Abfrage
auf Port 53 auf seinen DNSPort (`127.0.53.1`). Der Kill-Switch lässt nur Tors
eigenen Benutzer hinaus (`debian-tor`, UID 950, im Image fest; nur ein
Prozess mit `CAP_SETUID` könnte sie annehmen; ein MCP-Server hinter einem
Tor-Profil, dessen Image als UID 950 oder ein Tor-Benutzername läuft, wird
abgelehnt, da er diese Ausnahme teilen würde), über IPv4-TCP, mit Bridges nur
zu ihnen; anderes UDP und alles IPv6 werden verworfen. Alles geht über Tor
hinaus, auch die API-Verbindungen des Agenten selbst. `.onion`-Namen lösen
sich in `10.192.0.0/10` auf (AutomapHostsOnResolve), das die Sandbox
erreichen darf, sofern `allow_within` der Richtlinie es nicht ausschließt: mit
`--dns` jede `.onion`, die der Agent nachschlägt, ohne die `.onion`-Namen
seiner Freigabeliste. `--socks` fügt einen SocksPort auf `127.0.0.1:9050` in
der Sandbox hinzu (`IsolateDestAddr`) für Programme, die SOCKS sprechen: Tor
Browser mit `TOR_SKIP_LAUNCH=1`, Chromium oder Playwright mit
`--proxy-server=socks5://127.0.0.1:9050`, `curl --socks5-hostname`,
torsocks. Was darüber geht, geht am Verbindungs-Proxy vorbei (keine
Freigabeliste, keine Rückfragen, kein Verbindungsprotokoll; nur Tor trägt
es), deshalb ist er aus, bis man ihn verlangt. Die Bridges erscheinen nie im
Profil, im Plan, in den Protokollen oder einer Meldung, nur ihre Anzahl. Die
Prüfungen: tor muss innerhalb von 240 s zu 100 % gebootstrappt sein (der
Fehler sagt, wie weit es kam), `check.torproject.org/api/ip` muss für die von
außen gesehene Adresse `"IsTor": true` antworten, Tors DNSPort muss antworten
und einen `.onion`-Namen abbilden, eine an `1.1.1.1` geschickte Abfrage muss
ihn ebenfalls erreichen, und eine IPv6-Adresse muss unerreichbar bleiben.
`sandbox-ai images recipe tor --from claude` macht eine Image-Variante mit
torsocks und torbrowser-launcher (er lädt Tor Browser beim ersten Start und
prüft dessen Signatur; `--gui`). Grenzen: nur TCP (kein UDP, also kein QUIC:
Browser weichen auf TCP aus; kein ping, kein NTP), nur IPv4, und langsamer;
viele Seiten, auch der Anbieter des Agenten, können Tor-Exits ablehnen oder
mit Captchas prüfen. Der Exit-Relay sieht, was nicht verschlüsselt ist:
HTTPS verwenden. In der Sandbox öffnet der Verbindungs-Proxy TLS weiterhin
für seine Prüfung, wie ohne Tor (ein Programm hinter dem SocksPort sieht die
echten Zertifikate). `--no-proxy` wird abgelehnt. Tors DNSPort ist für den
Agenten geschlossen, außer der Start hat `--dns` oder `--egress-all-allow`;
`sandbox-ai net allow --dns` während eines Laufs öffnet ihn nicht, und ein
während eines Laufs erlaubter Name wird auf dem Host nachgeschlagen, ein
`.onion`-Name also nicht (mit ihm starten, oder mit `--dns`).

**Geheimnisse.** Private und Preshared Keys, OpenVPN-Zugangsdaten und
Auth-Keys liegen in `sandbox-ai secrets` (Schlüsselbund oder `0600`-Dateien);
die Profile in `~/.config/sandbox-ai/vpn` (`0600`) halten den Rest, ein
OpenVPN-Profil seine Konfiguration samt Client-Schlüssel (ebenfalls `0600`). Nur
der VPN-Container bekommt sie, über seine Standardeingabe: nie Dateien, Umgebung
oder Logs des Agenten oder eines MCP-Servers und nie den Dry-Run-Plan (der
Profil, Endpunkte, Schnittstelle und Prüfungen zeigt). Sobald es Profile gibt,
darf kein Workspace, kein Mount (auch kein schreibgeschützter), kein Kit, kein
`--copy` und keine Datei-Option diese Verzeichnisse oder die Geheimnis-Dateien
einer Sandbox zeigen, und ein Workspace oder Mount, über den der Agent ein
Profil ändern könnte, wird abgelehnt.

**MCP-Server.** Ein Server mit Befehl bekommt `--vpn PROFIL` in seinen
`options`; ein Katalog-Server mit Netz bekommt eines mit `sandbox-ai mcp add NAME
--network full --vpn PROFIL` (oder `"vpn": "PROFIL"` in `mcp.json`); `sandbox-ai
mcp start NAME --vpn PROFIL` setzt für diesen Start eines davor. Die Container
eines Katalog-Servers treten dem Namespace des VPN-Containers bei (`--network
container:...`); eine fehlgeschlagene Prüfung stoppt den Server.

**LLM-Server.** Ein [LLM-Server](#llm-server-lokal-und-entfernt), der mit
`sandbox-ai llm add NAME ... --vpn PROFIL` angelegt wurde, bringt sein VPN mit:
`--llm NAME` schließt `--vpn PROFIL` ein, und der Name des Servers wird wie die
anderen erlaubten Namen durch den Tunnel aufgelöst.

**Grenzen.** Bubblewrap wird nicht unterstützt (`--vpn` mit `--backend bwrap`
wird abgelehnt, siehe TODO.md). Die Prüfungen laufen einmal beim Start.
`--allow-host-port` erreicht weiterhin das Loopback des Hosts, am Tunnel
vorbei, wie verlangt. Namen, die während eines Laufs erlaubt werden
(`sandbox-ai net allow --allow-host`, Rückfragen), werden auf dem Host
aufgelöst; die Verbindungen selbst gehen durch den Tunnel. Ein MCP-Server hat
ein eigenes Netz: Geben Sie ihm ebenfalls `--vpn` (`require_vpn` tut das). Das
Board der Desktop-App behält `--vpn` als Option der Befehlszeile, bis es einen
VPN-Knoten hat.

## LLM-Server: lokal und entfernt

Jeder Agent kann statt mit der Cloud mit einem Modell auf Ihrem eigenen Server
arbeiten: **Ollama**, **LM Studio**, dem Server von **llama.cpp**
(`llama-server`), **vLLM** oder einem anderen **OpenAI-kompatiblen** Server
(etwa einem LiteLLM-Gateway), auf diesem Rechner, im LAN, hinter einem VPN oder
im Internet. Sie legen ihn einmal unter einem Namen an; sein Schlüssel bleibt
in `sandbox-ai secrets`:

```bash
sandbox-ai secrets set GPU_KEY            # API-Schlüssel des Servers, falls er einen hat
sandbox-ai llm add gpu --kind vllm --url https://gpu.example.org:8443 --api-key-secret GPU_KEY
sandbox-ai llm models gpu                 # was er gerade anbietet
sandbox-ai llm check gpu --agent codex    # Adresse, TLS, Schlüssel, Modell, die API für Codex
sandbox-ai codex --llm gpu:Qwen/Qwen3-Coder-30B --no-provider-defaults
```

`--llm NAME[:MODELL]` öffnet genau Adresse und Port dieses Servers,
konfiguriert den Agenten dafür und gibt ihm **statt des Schlüssels einen
Platzhalter**. Ohne `:MODELL` nimmt der Start das Modell des Endpunkts
(`llm add --model`) oder das einzige, das der Server aufgelistet hat. Mit
`--no-provider-defaults` erreicht der Agent nichts außer dem Server (und dem,
was andere Optionen erlauben). Die Cloud-Anmeldung des Agenten wird nicht
hineinkopiert (`--copy-auth` tut es weiterhin), und `--llm` ersetzt
`--local-config`. Der Wizard bietet angelegte Server für jeden Agenten an,
listet ihre Modelle und schreibt `--llm NAME:MODELL`; auch eine Projektdatei
kann einen nennen, und die Richtlinie des Administrators kann `--llm`
verbieten oder wie jedes andere Ziel mit `allow_within`/`allow_hosts`
begrenzen.

| Agent | API | Was sandbox-ai schreibt | Server mit dieser API |
|---|---|---|---|
| OpenCode | Chat Completions (`/v1/chat/completions`) | `~/.config/opencode/sandbox-ai-llm.json` (Provider `llm-NAME` über `@ai-sdk/openai-compatible`, alle Modelle, die der Server gelistet hat) und `OPENCODE_CONFIG` | alle |
| Codex | Responses (`/v1/responses`; Codex hat `wire_api = "chat"` in 0.95.0 entfernt) | `~/.codex/sandbox-ai-llm.config.toml` (Provider `llm-NAME`, `wire_api = "responses"`, `env_key` mit dem Platzhalter; Update-Prüfung, Analytics, Metriken, Plugin-Abgleich und die gehostete Websuche aus), gestartet mit `--profile sandbox-ai-llm` (Codex 0.134 oder neuer) | Ollama 0.13.3, LM Studio 0.3.29, llama.cpp b7793 (b9161 für Codex' Werkzeugtypen), vLLM 0.10.0 (gestreamte Werkzeugaufrufe 0.18.0) |
| Claude Code | Anthropic Messages (`/v1/messages`) | `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN` (der Platzhalter), `ANTHROPIC_MODEL` und die Modellvariablen für Opus, Sonnet, Haiku und Subagenten, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`; `skipWebFetchPreflight` in seinen Einstellungen | Ollama 0.14.0, LM Studio 0.4.1, llama.cpp b7187, vLLM 0.11.1 (und neuer); ein anderer Server nur mit `llm add --anthropic` |

Claude Code spricht nur die Anthropic-Messages-API: Ein Start von Claude mit
einem allgemeinen `openai-compatible`-Server wird abgelehnt, und die Meldung
nennt die Alternativen (Codex oder OpenCode damit, einer der vier Server oben,
oder ein Server mit `/v1/messages`, angelegt mit `--anthropic`). Die
Versionsnummern sind die, mit denen der jeweilige Server die API eingeführt
hat; `sandbox-ai llm check NAME --agent AGENT` fragt den Server selbst (eine
leere Anfrage, es wird nichts erzeugt).

**URLs.** `--url` nennt die Wurzel des Servers, mit Pfadpräfix, wenn ein
Gateway eines davorsetzt (`https://gw.example.org/ollama`); jeder Agent hängt
seinen API-Pfad an, ein abschließendes `/v1` wird weggelassen. IPv4, IPv6
(`https://[2001:db8::5]:8443`) und Hostnamen gehen. `https` wird gegen die CAs
des Systems geprüft oder gegen `--ca DATEI` (kopiert nach
`~/.config/sandbox-ai/llm/NAME.pem`; der Proxy vertraut ihr nur für diesen
Server). Klartext-`http` geht nur

- an das Loopback dieses Hosts (`127.0.0.1`, `localhost`, `[::1]`): nur
  Podman, impliziert `--allow-host-port PORT`; der Agent nutzt `127.0.0.1`,
  der Server muss also dort lauschen;
- an private Adressen (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`),
  CGNAT und Tailscale (`100.64.0.0/10`), link-lokales IPv4 und unique-local
  IPv6 (`fc00::/7`, einschließlich Tailscales `fd7a:115c:a1e0::/48`);
- an einen Hostnamen, dessen Adressen alle zu diesen gehören (geprüft bei
  `llm add` und bei jedem Start).

Alles andere über Klartext-`http` braucht `llm add --insecure-http` und wird
bei jedem Start deutlich gewarnt: Prompts, Code und Schlüssel liefen
unverschlüsselt durch Netze, die Sie nicht kontrollieren.

**Netz.** Eine Adresse wird wie `--allow-tcp IP:PORT` geöffnet, ein Name auf
Port 443 wie `--allow-host NAME`; ein Name auf einem anderen Port wird beim
Start festgelegt und nur auf diesem Port geöffnet (TCP/443 bleibt zu). Mit
Pfadpräfix auf einem Hostnamen lässt der Proxy nur Anfragen unterhalb des
Präfixes zu diesem Host durch (eine Regel `--allow-http 'HOST * /PRÄFIX/**'`).

**Der Schlüssel bleibt außerhalb der Sandbox.** `--api-key-secret GEHEIMNIS`
nennt ein Geheimnis aus `sandbox-ai secrets`; die Endpunktdatei
(`~/.config/sandbox-ai/llm.json`, Modus `0600`) enthält nur seinen Namen. Beim
Start kommt der Schlüssel in `proxy.json` im Verzeichnis des
Verbindungs-Proxys, das der Agent nicht lesen kann. Die Konfiguration des
Agenten enthält den Platzhalter `sandbox-ai-llm-key-placeholder`; der Proxy
ersetzt ihn in `Authorization`, `x-api-key` oder `api-key` durch den Schlüssel
(und setzt `Authorization: Bearer SCHLÜSSEL`, wenn der Agent keine
Anmeldedaten schickt), und zwar nur für Anfragen an diesen Server: über sein
Schema und seinen Port, an den Namen, dessen Zertifikat er geprüft hat
(`https`), oder an seine festgelegte Adresse (`http`), unterhalb seines
Pfadpräfixes. Eine Anfrage anderswohin behält den Platzhalter.
`--header-secret HEADER=GEHEIMNIS` fügt auf dieselbe Weise Header hinzu, etwa
`CF-Access-Client-Id` und `CF-Access-Client-Secret` eines
Cloudflare-Access-Gateways. Der Schlüssel steht weder in der Umgebung noch in
den Dateien des Agenten, im Plan von `--dry-run` oder im Verbindungsprotokoll
(das Protokoll lässt die genauen Werte aus, selbst wenn ein Server einen
zurückschickt; die Verbindung trägt `"llm": "NAME"`). Weil der Proxy ihn
einsetzt, lehnt ein Endpunkt mit Schlüssel, Header-Geheimnissen oder eigener
CA `--no-proxy` und `--no-inspect` für seinen Host ab. Grenzen: Ein Server,
der Anfrage-Header in seinen Antworten zurückgäbe, zeigte dem Agenten den
Schlüssel (die vier Server oben tun das nicht); über Klartext-`http` geht der
Schlüssel unverschlüsselt durch Ihr LAN.

**VPN.** `llm add --vpn PROFIL` nennt das [VPN](#vpn)-Profil, über das ein
Server erreicht wird. `--llm NAME` startet die Sandbox dann dahinter, wie es
`--vpn PROFIL` täte (nur Podman, mit Kill Switch und Prüfungen), und löst den
Namen des Servers über den Resolver des VPNs auf, sodass auch ein Name
funktioniert, den nur das VPN kennt. Ein Start mit einem anderen `--vpn` wird
abgelehnt (eine Sandbox hat ein VPN); eine Richtlinie, die `--vpn` verbietet,
lehnt einen solchen Endpunkt ab, und eine, die ein VPN verlangt, lässt ihn
gelten. `llm check` und `llm models` laufen weiterhin aus dem eigenen Netz des
Hosts.

```bash
sandbox-ai llm add NAME --kind ollama|lmstudio|llamacpp|vllm|openai-compatible --url URL \
    [--api-key-secret GEHEIMNIS] [--header-secret HEADER=GEHEIMNIS] [--ca DATEI] [--vpn PROFIL] \
    [--model MODELL] [--anthropic] [--insecure-http] [--replace]
sandbox-ai llm list [--json]
sandbox-ai llm show NAME [--json]
sandbox-ai llm remove NAME              # die Geheimnisse bleiben in sandbox-ai secrets
sandbox-ai llm models NAME [--sort name|size|loaded] [--json]
sandbox-ai llm check NAME [--agent claude|codex|opencode] [--model MODELL]
```

| Server (`--kind`) | Standardport | Modelle aus | Größe | Geladen |
|---|---|---|---|---|
| Ollama (`ollama`) | 11434 | `/api/tags`, `/api/ps` | ja | ja |
| LM Studio (`lmstudio`) | 1234 | `/api/v0/models` (Rückfall `/v1/models`) | falls gemeldet | ja |
| llama.cpp (`llamacpp`) | 8080 | `/v1/models` | ja | ja (Router-Modus: je Modell) |
| vLLM (`vllm`) | 8000 | `/v1/models` | nein | immer (bereitgestellt = geladen) |
| andere (`openai-compatible`) | keiner | `/v1/models` | nein | unbekannt |

Die Auflistung schickt den Schlüssel als Bearer-Token und die
Header-Geheimnisse mit, nutzt die CA des Endpunkts, keinen HTTP-Proxy des
Hosts, folgt keiner Umleitung (sie trüge den Schlüssel anderswohin) und gibt
nach 10 Sekunden auf; die Modellnamen werden für den nächsten Start gemerkt.

### OpenCode mit einem Server im LAN, ohne ihn anzulegen

Der Wizard kann eine lokale OpenCode-Konfiguration auch direkt schreiben (der
Weg vor `sandbox-ai llm`, für Server ohne Schlüssel): Er listet die Modelle mit
Name, Größe und Ladezustand, jeweils sortierbar, und schreibt alles Folgende.
Von Hand:

```bash
./sandbox-ai wizard models --kind ollama --host 192.168.1.20 --sort size
./sandbox-ai opencode --workspace "$HOME/src/demo" \
  --local-config ./opencode-local.json --allow-tcp 192.168.1.20:11434
```

`--local-config` kopiert die OpenCode-Konfiguration privat, entfernt alle
Standard-Cloud-Freigaben und deaktiviert Projektkonfiguration,
Modellkatalog-Abruf, Auto-Updates und Teilen. `--allow-tcp` öffnet genau den
Port des Servers. Die vom Wizard erzeugte Konfiguration nutzt OpenCodes
OpenAI-kompatiblen Provider (`http://IP:PORT/v1`). Auf diesem Weg erreicht die
Sandbox `localhost` des Hosts nicht: den Server ans LAN binden
(`OLLAMA_HOST=0.0.0.0`, in LM Studio „Serve on Local Network“,
`llama-server --host 0.0.0.0`, `vllm serve … --host 0.0.0.0`) und die LAN-IP
des Hosts verwenden, oder ihn mit `sandbox-ai llm add` als
`http://localhost:PORT` anlegen.

## Lokale MCP-Server

Lokale MCP-Server laufen **in einer eigenen Sandbox** (Podman-Pod oder
Bubblewrap, gleicher Firejail-Guard), ohne Anmeldedaten und, sofern ihre
Definition nichts anderes sagt, ohne Netz. `--mcp NAME` startet die
Server-Sandbox, falls sie noch nicht läuft, und verbindet dann den Agenten; der
Server läuft für weitere Agenten und Sessions weiter.

```bash
./sandbox-ai claude --workspace "$HOME/src/app" --mcp demo   # eingebauter Testserver
./sandbox-ai mcp list                                        # NAME  STATUS  SERVES  COMMAND
./sandbox-ai mcp stop demo
```

### Ein MCP-Server für einen bestehenden Start

```bash
./sandbox-ai mcp attach demo web               # web bekommt demo (auch für seine späteren Starts)
./sandbox-ai mcp attach demo web --restart     # web läuft: jetzt damit neu gestartet
./sandbox-ai mcp attach demo web --live        # web läuft: demo erreicht es jetzt, ohne Neustart
./sandbox-ai mcp attach --helper codex web --live   # ein Codex-Helfer für das laufende web
./sandbox-ai mcp detach demo web --live        # sein Relay in web schließt jetzt; aus den Optionen
./sandbox-ai mcp start demo --for web          # demo so starten, wie web es startet, und verbinden
./sandbox-ai resume web --mcp demo             # dasselbe für einen gestoppten Start, jetzt gestartet
```

`mcp attach NAME START` (und `mcp start NAME --for START`) fügt `--mcp NAME` zu
den Optionen des behaltenen Starts hinzu, geprüft wie sein Start es prüft: die
Richtlinie des Administrators (`forbid` `--mcp`), sein Sicherheitsmodus
(`paranoid` lehnt einen Server mit Netz ab) und die Definition des Servers.
`--for` startet den Server sofort mit Backend und Regeln dieses Starts, sodass
der Start ihn laufend vorfindet. Ohne `--live` (unten) erreicht eine laufende
Sandbox einen neuen MCP-Server erst nach einem Neustart: Ihr Agent liest seine
MCP-Server beim Start (`--mcp-config`, `-c mcp_servers`, `opencode.json`).
Darum wird ein laufender Start damit neu gestartet (`--restart`, oder ja im
Terminal; der Agent setzt sein Gespräch fort), sonst erreicht er den Server ab
seinem nächsten Start, und der Befehl sagt, wie man ihn neu startet oder mit
`--live` verbindet. Ein Lauf ohne
behaltenen Start (`--no-keep`) hat nichts zum erneuten Starten: beenden und mit
`--mcp NAME` neu starten. `mcp list` und `sandbox-ai list` zeigen, wen jeder
Server bedient; die MCP-Seite der App hat **Verbinden mit…**, die
Weboberfläche dasselbe in der Zeile eines Servers unter Starts und Sitzungen.

**Während des Laufs: `--live`.** `mcp attach NAME START --live` (oder `--helper
AGENT` statt NAME) gibt einem laufenden Start den Server ohne Neustart. Jede
Prüfung eines Starts gilt (die Richtlinie des Administrators, der
Sicherheitsmodus des Starts und der, den sein Lauf hat, die Definition des
Servers, sein Netz und seine Ordner); die Sandbox des Servers wird gestartet,
wie dieser Start sie starten würde, falls sie nicht läuft. Dann öffnet der
Launcher das Socket-Verzeichnis des Servers und reicht diesen Deskriptor (keinen
Pfad, keinen Mount) über den Kontroll-Socket an den Supervisor des Laufs, der
das Relay des Servers unter `/run/sandbox-mcp/NAME/server.sock` öffnet wie bei
einem Start: dasselbe Gateway, die Fragen des Laufs (`sandbox-ai approvals`),
Taint und die Regeln für getaintete Läufe, `mcp-calls.jsonl` (mit einem
Eintrag `attached` und später `detached`). Der Supervisor prüft Richtlinie und
Modus auch selbst und verbindet sich mit dem Socket des Servers, ohne einem
Link zu folgen, den der Server in sein Verzeichnis gelegt haben könnte. Der
Lauf vermerkt ihn (`run.json` `mcp_servers`: `sandbox-ai list`, `mcp list`),
der Start behält ihn (`--mcp NAME` oder `--helper AGENT`), und die
MCP-Konfiguration des Agenten bekommt ihn dort, wo der Agent eine liest:

| Agent | Was `--live` für ihn schreibt | Was Sie tun |
|---|---|---|
| Claude Code | `mcpServers.NAME` in `~/.claude.json` der Sandbox (User-Scope; am Ende des Laufs wieder entfernt: der nächste Start nennt ihn auf seiner Kommandozeile) | `/mcp` öffnen und nach NAME sehen. Claude Code liest seine MCP-Server beim Start; ob ein laufendes einen seitdem hinzugefügten Server übernimmt, ist nicht dokumentiert, darum, falls `/mcp` ihn nicht zeigt, den Agenten neu starten: `sandbox-ai resume START --restart` (das Gespräch geht weiter) |
| Codex | nichts: es liest seine MCP-Server nur beim Start (`-c mcp_servers`) | den Agenten neu starten: `sandbox-ai resume START --restart` (das Gespräch geht weiter) |
| OpenCode | nichts: es liest seine MCP-Server nur beim Start (`OPENCODE_CONFIG_CONTENT`) | ebenso |

`mcp detach NAME START --live` schließt das Relay sofort (die Verbindungen des
Agenten dazu enden, seine Aufrufe schlagen fehl; der Agent nennt den Server
womöglich bis zu seinem Neustart weiter), nimmt ihn aus `run.json`, der
Konfiguration des Agenten und den Optionen des Starts; ohne `--live` nur aus
den späteren Starts. Keines von beiden beendet die Sandbox des Servers (andere
Starts nutzen sie vielleicht: `mcp stop NAME`). Abgelehnt für einen pausierten
Start (erst fortsetzen), einen Lauf einer älteren Version (er hat keinen
schreibgeschützten Ort für später geöffnete Relays: `--restart`), einen Lauf
auf einem anderen Rechner (`--podman-url`) und einen Lauf ohne behaltenen
Start. Mit `--json` geben beide `{"server", "start", "run", "state",
"attached": "live" | "already" (attach) / "detached": "live" | "next start" |
"not attached" (detach), "agent", "agent_config", "agent_restart": "maybe" |
"required", "relay", "kept" / "relay_closed", "removed_from_options",
"message"}` aus.

Zwischen den Sandboxes gibt es keinen Netzweg: Die Server-Sandbox lauscht auf
einem Unix-Socket in `~/.local/state/sandbox-ai/mcp/NAME/sock/` und startet je
Verbindung eine Instanz des stdio-Servers; die Agent-Sandbox bindet dieses
Verzeichnis schreibgeschützt ein und spricht über eine kleine stdio-Bridge
damit. Der Starter meldet jeden Server beim Agenten an: Claude per
`--mcp-config`, Codex per `-c mcp_servers.NAME…`, OpenCode per
`OPENCODE_CONFIG_CONTENT`.

Der eingebaute Server **`demo`** ist ein Testserver ohne Abhängigkeiten mit zwei
Tools: `list_directory(path)` (Standard `/work` der Server-Sandbox) und
`version`, das seine Version zusammen mit Hostname, Kernel, PID-Namespace und
Seccomp-Zustand meldet und so zeigt, dass er eingesperrt in einer eigenen
Sandbox läuft.

Weitere Server in `~/.config/sandbox-ai/mcp.json` oder in einer vertrauten
Projektdatei unter `"mcp_servers"` definieren:

```json
{
  "files": {"command": ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/work"],
            "workspace": "/home/ich/src/app", "options": ["--allow-host", "registry.npmjs.org"]},
  "fetch": {"command": ["uvx", "mcp-server-fetch"], "options": ["--allow-pypi", "--egress-all-allow"]}
}
```

`command` läuft in der Server-Sandbox; `options` sind Starteroptionen dafür
(Netz, Mounts; `--backend` und `--podman-url` sind die des Agenten und nicht setzbar); `workspace` wird als `/work`
eingebunden (Standard: ein leeres privates Verzeichnis); `agent` wählt das Image
(Standard `codex`; jedes Image hat Node.js, Python und uv). Agent-Logins,
Git-Zugangsdaten und -Identität, Sessions, tmux, `--binary` und verschachteltes
`--mcp` sind in einer Definition nicht erlaubt; einen API-Schlüssel, den der
Server braucht, bekommt er mit `--secret-env NAME` (oder `--env`), und nur
diese Variable. Ein Server, der sein Paket beim Start lädt (`npx`, `uvx`),
braucht die passende Registry-Freigabe. Pfade in einer Definition (`workspace`,
`--mount` ...) müssen absolut sein oder mit `~` beginnen: Ein laufender Server
wird von allen Agenten geteilt, die ihn nutzen.

### Server aus einem MCP-Katalog

Fertige Server gibt es als Container-Images aus
[sandbox-ai-mcp](https://github.com/c0decave/sandbox-ai-mcp) (Suche und Web: DuckDuckGo, Brave
Search, Webseiten als Text; Ghidra und Playwright vorbereitet) und
[axross-mcp-hub](https://github.com/c0decave/axross-mcp-hub) (Sicherheitswerkzeuge: radare2,
capa, binwalk, YARA, tshark, Nmap, nuclei und mehr). Beide sind auditiert und per Digest fixiert.

```bash
sandbox-ai mcp catalog                               # Einträge beider Kataloge
sandbox-ai mcp add duckduckgo --network full         # per Digest holen, Definition schreiben
sandbox-ai mcp add brave-search --network full --env BRAVE_API_KEY
sandbox-ai mcp add hub:binwalk --mount /work=~/firmware:ro --mount /out=~/entpackt:rw
sandbox-ai claude --mcp duckduckgo --mcp binwalk
```

`add` lehnt einen Eintrag ohne Digest ab, holt `IMAGE@DIGEST` und schreibt eine Definition mit
`"image"`, dem Netz, den Ordnern (`-m ZIEL=HOST[:ro|rw]`) und den Namen der durchzureichenden
Variablen (`-e NAME`; der Wert kommt beim Start aus Ihrer Umgebung und wird nie gespeichert).
Jede Verbindung eines Agenten startet einen Container mit der gehärteten Run-Line, mit der beide
Projekte ihre Images testen: `podman run --rm -i --init --pull=never --cap-drop=ALL
--security-opt=no-new-privileges --read-only`, ein kleines `noexec`-/tmp, `--memory=512m`
(`-M` ändert es), `--pids-limit=128` (`-p`), `--network=none` oder dasselbe private
pasta-Netz wie die Agenten. Ein Katalogeintrag, der nur einige Hosts braucht (*Allowlist*),
bekommt das volle äußere Netz nur, wenn Sie ihn mit `--network full` hinzufügen: sandbox-ai kann
einen Server noch nicht auf Hosts beschränken. Die Sandbox Ihres Agenten behält ihre Firewall:
Der Server hat das Netz oder die Ordner, der Agent nur den Socket. Was der Server tut,
bestimmt aber der Agent: Ein Fetch- oder Browser-Server mit Netz schickt, was der Agent
verlangt, an dessen Firewall, Proxy und Rückfragen vorbei. Geben Sie einem Server nur dort
Netz, wo Sie es auch dem Agenten geben würden. Höchstens 16 Verbindungen zu einem Server
laufen gleichzeitig. Die Host-Ordner eines Image-Servers durchlaufen dieselben
Prüfungen wie die Mounts des Agenten: kein Symlink auf dem Weg, den eine
Sandbox angelegt haben könnte, kein privater Ort für `rw` und nicht innerhalb
eines Baums, den ein Agent schreiben kann; seine `rw`-Ordner werden wie
Workspaces vermerkt.

**Signierte Kataloge.** `add` nimmt einen Eintrag nur aus einem Katalog,
dessen `catalog.json.sig` gegen einen Schlüssel in `sandbox/mcp-signers` prüft
(eine SSH-Signatur im Namensraum `sandbox-ai-mcp`, geprüft mit `ssh-keygen -Y
verify`, wie bei Releases). Die Signatur deckt jeden Image-Digest und
Rootfs-Hash des Katalogs ab; `scripts/sign_catalog.py` von sandbox-ai-mcp setzt
sie nur für Server, die aus ihren gepinnten Quellen gebaut, geprüft und deren
Smoke-Tests mit den Startzeilen von sandbox-ai unter Podman und Bubblewrap
bestanden sind. `sandbox-ai mcp catalog` zeigt je Eintrag `signed` oder
`unsigned`; eine Definition hält den Schlüssel fest, der sie signiert hat
(`signed_by`). Ein unsignierter Katalog (ein eigener) braucht `--unsigned`;
eine Signatur, die nicht prüft, wird immer abgelehnt.

**Bubblewrap.** Ohne Podman (oder mit `--backend bwrap`) nimmt `add` statt des
Images das Rootfs des Eintrags (aus `rootfs.json` neben dem Katalog, signiert
wie er, da axross den Katalog selbst liest): ein Archiv desselben Images, gegen den SHA-256
aus dem signierten Katalog geprüft, bevor irgendetwas davon entpackt wird, und
entpackt, ohne ihm zu trauen (keine absoluten Pfade, kein `..`, keine Geräte,
keine setuid-Bits, nichts durch einen Link geschrieben) nach
`~/.local/share/sandbox-ai/mcp-rootfs/SHA256/`, schreibgeschützt. Jede
Verbindung läuft dann als `bwrap --unshare-all --uid U --ro-bind ROOTFS /
--proc /proc --dev /dev --tmpfs /tmp --cap-drop ALL --clearenv
--die-with-parent --new-session` mit den Ordnern und Variablen des Eintrags,
in einem systemd-Scope mit Speicher- und Prozessgrenzen, und für Netz `full`
mit slirp4netns ohne das Loopback des Hosts (wie pasta für die Container). Die
Argumente gehen über eine Pipe an bwrap (`--args`), der Wert einer Variable
steht so nie in einer Befehlszeile. `--backend both` behält beide Formen; ein
Lauf mit `--backend bwrap` nimmt dann das Rootfs.

Das Relay vor jedem Server ist auch ein **Gateway**: `mcp`-Regeln in einem
[Regelwerk](#regelwerke) erlauben, erfragen oder verbieten jeden
Werkzeugaufruf nach Werkzeug und Argumenten, `tools/list` lässt die Werkzeuge
weg, die immer verboten sind, und jeder Aufruf steht mit Entscheidung und
Ergebnis in `~/.local/state/sandbox-ai/mcp/NAME/mcp-calls.jsonl`.
JSON-RPC-Batches werden abgewiesen (MCP kennt seit 2025-06-18 keine).

### Ein anderer Agent als Helfer

`--helper AGENT` gibt dem Agenten einen zweiten (oder dritten) Agenten, dem
er Aufgaben übergibt. Claude Code, Codex und OpenCode können jeweils führen
und jeweils helfen:

```bash
sandbox-ai claude --workspace ~/src/app --helper codex --helper opencode
sandbox-ai codex --helper claude
```

Jeder Helfer läuft als MCP-Server in einer eigenen Sandbox, anders als andere
MCP-Server mit allem, was ein Agentenstart hat:

- seiner Anmeldung und nur den Hosts seines Anbieters;
- seinen Kits, dem Projektgedächtnis und den Plugins;
- den `--rules` des Laufs und den `rules` des Projekts für seine eigenen
  Werkzeuge, mit seinen verwalteten Hooks (und der Versionsprüfung von
  Codex);
- `--dangerously-skip-permissions`, wenn der führende Agent es hat.

Was den führenden Agenten einschränkt, schränkt auch seinen Helfer ein:
`--workspace-ro`, der Sicherheitsmodus und `--no-auth`. `--helper` wird mit
`--offline` und `--llm` abgelehnt (ein Helfer erreicht seinen eigenen
Anbieter) und in den Modi `high` und `paranoid`. Eine Policy des
Administrators, die `--mcp` verbietet, verbietet es auch.

Er arbeitet im Hauptarbeitsordner des führenden Agenten (`/work`). Es gibt
einen Helfer je Agent und Hauptarbeitsordner (`helper-AGENT-HASH` in
`sandbox-ai mcp list`): Ein anderes Projekt bekommt nie den Helfer dieses
Projekts, und zwei führende Agenten im selben Arbeitsordner teilen ihn sich.

Der führende Agent sieht ein Werkzeug, `ask(prompt, continue)`. Der Helfer
erledigt die Aufgabe in `/work` in seinem nicht interaktiven Modus und
antwortet mit seiner letzten Nachricht: `claude -p`, `codex exec` (mit
`--sandbox danger-full-access`, wie jedes Codex hier), `opencode run`. Mit
`continue` macht er in seinem letzten Gespräch weiter. Der Aufruf geht wie
jeder MCP-Aufruf über das Relay: `mcp`-Regeln, Rückfragen, der Taint des
Laufs und `mcp-calls.jsonl` gelten für ihn. Ein führendes Codex wartet bis zu
2000 Sekunden auf ihn (`tool_timeout_sec`).

- **Eine Aufgabe nach der anderen.** Eine Sperre im HOME des Helfers gilt
  über alle Verbindungen. Die Aufgabe eines zweiten führenden Agenten wird
  als „busy“ abgelehnt, solange eine läuft.
- **30 Minuten.** Eine Aufgabe, die länger läuft, wird beendet.
- **Weniger Einstellungen vom führenden Agenten.** Er teilt `/work`, deshalb
  liest der Helfer dort keine Projekteinstellungen und keine
  Projekt-MCP-Server. Claude läuft mit
  `--setting-sources user --strict-mcp-config`, Codex vertraut dem Ordner
  nicht, und OpenCode läuft mit `OPENCODE_DISABLE_PROJECT_CONFIG=1`. Git
  führt dort keinen fsmonitor, keine Hooks des Repositorys und keine
  `.gitattributes`-Filter aus (die Filter ab Git 2.42).

Grenzen:

- Beide Agenten schreiben in denselben Arbeitsordner.
- Der Helfer liest weiterhin die Anweisungen in `/work` (`AGENTS.md`,
  `CLAUDE.md`) und tut, worum der führende Agent ihn bittet, und andere
  Git-Einstellungen in `/work/.git/config` (Aliase, `core.sshCommand`,
  Diff-Treiber) können weiter Code ausführen, wenn er sie nutzt. Ein führender
  Agent, dem Sie nicht trauen, kann seinen Helfer mit Worten lenken. Geben
  Sie einem Helfer nur, was Sie dem führenden Agenten geben würden,
  einschließlich seiner Anmeldung.
- Ein Claude-Helfer ohne `--dangerously-skip-permissions` lehnt Werkzeuge
  ab, die Ihre Freigabe bräuchten, denn niemand kann seine Fragen
  beantworten. Codex- und OpenCode-Helfer handeln in jedem Fall ohne
  Rückfrage: Die eigene Sandbox von Codex lässt sich in der von sandbox-ai
  nicht aufbauen, und die Sandbox des Helfers ist die Grenze.
- Die Befehle der Helfer sind mit Claude Code 2.1 geprüft. Die von Codex und
  OpenCode folgen deren Dokumentation und liefen hier noch nicht.

### Andere Agenten in derselben Sandbox

`--with AGENT` holt andere Agenten in die Sandbox des Agenten, damit er sie
aus der Shell aufruft:

```bash
sandbox-ai build team                                   # einmal: alle drei Agenten in einem Image
sandbox-ai claude --with codex --with opencode
```

In der Sandbox ruft der führende Agent `codex exec -- "AUFGABE"`,
`opencode run -- "AUFGABE"` oder `claude -p -- "AUFGABE"` auf, und
`$SANDBOX_AI_COMPANIONS` nennt die Agenten, die da sind. Jeder Agent mit
--with bekommt:

- **Seine Anmeldung,** kopiert wie die automatische Anmeldung des führenden
  Agenten, und `--no-auth` gilt auch für ihn. Ein `--copy` seiner
  Anmeldedatei behält stattdessen dieses Konto. Eine erneuerte
  Claude-Anmeldung wird auf den Host zurückgeschrieben, erneuerte Codex- und
  OpenCode-Anmeldungen in den Login-Store von sandbox-ai.
- **Die Hosts seines Anbieters.** Sie gelten als Anbieter dieses Laufs, für
  die Regeln und den Taint.
- **Regel-Hooks:** die Regel-Hooks der Regeln des Laufs in seiner
  verwalteten Konfiguration, und die Versionsprüfung von Codex.
- **Codex:** `sandbox_mode = "danger-full-access"` in `~/.codex/config.toml`,
  außer die Datei wählt etwas anderes, wie der Launcher es dem führenden
  Codex gibt.

`--with` wird mit `--offline` und `--llm` abgelehnt und in den Modi `high`
und `paranoid`: Es öffnet einen weiteren Anbieter. Der Start nimmt
`localhost/team-sandbox:latest`, außer `--image` nennt ein anderes. Mit
Bubblewrap werden die Programme des Hosts genommen.

Verglichen mit [Helfern](#ein-anderer-agent-als-helfer) ist das einfacher und
schwächer:

- Alle Agenten teilen ein Netz, ein HOME und einen Arbeitsordner.
- Die Kits sind nur die des führenden Agenten.
- Die Regel-Hooks von OpenCode kommen über `OPENCODE_CONFIG_CONTENT`, das
  der führende Agent weglassen kann, wenn er OpenCode startet (wie jeder
  zweite Agent, der aus Bash gestartet wird).

### Ein Team von Agenten

`sandbox-ai team` startet je Agent eine Sandbox auf einem gemeinsamen
Arbeitsordner, und sie arbeiten über ein Aufgabenbrett zusammen:

```bash
sandbox-ai team start app --agents claude,codex,opencode -w ~/src/app -- --allow-pypi
sandbox-ai attach app-claude                    # mit dem führenden Agenten sprechen
sandbox-ai team show app                        # Mitglieder, ihr Zustand, das Brett
sandbox-ai team post app codex "run the tests"  # eine Aufgabe von Ihnen
sandbox-ai team stop app                        # alle Mitglieder und das Brett
sandbox-ai team delete app --yes                # mit den HOMEs der Mitglieder
```

Jedes Mitglied ist eine benannte Sitzung (`app-claude`, `app-codex`, ...),
die Sie wie jeden Start anhängen, anhalten, fortsetzen oder beenden. Die
Optionen nach `--` gelten für jedes Mitglied. Ein Team übernimmt nie eine
Sitzung, deren Name schon vergeben ist. `stop` und `delete` enden mit Status
1, wenn ein Mitglied nicht beendet oder gelöscht werden konnte, und `delete`
behält das Team dann.

Der erste Agent führt. Er startet mit einer Anweisung, die sein Team und die
Werkzeuge des Bretts nennt. Die anderen starten mit der Anweisung, auf
Aufgaben zu warten, sie zu erledigen und zu berichten.

Das Brett ist der MCP-Server `team-NAME` in einer eigenen Sandbox ohne Netz.
Er hat diese Werkzeuge:

- `post(to, task)`;
- `wait_for_task(agent)`;
- `done(id, answer)`;
- `wait_for_answer(id)`;
- `board()`.

Jedes Mitglied erreicht es über sein Relay, also gelten `mcp`-Regeln,
Rückfragen und `mcp-calls.jsonl`. Seine Datei liegt in
`~/.local/state/sandbox-ai/teams/NAME/board/`, und `team show` gibt seinen
Text mit maskierten Steuerzeichen aus.

- **Warten:** Ein Warten dauert standardmäßig 50 Sekunden.
- **Verlorene Aufgaben:** Eine Aufgabe, die ein Mitglied genommen, aber nie
  berichtet hat, kommt bei seinem nächsten `wait_for_task` wieder, falls die
  Antwort, die sie ausgab, verloren ging.
- **Größe:** Das Brett hält bis zu 1000 Aufgaben und 48 MiB. Erledigte
  Aufgaben machen Platz, offene nie.

Grenzen:

- Die Mitglieder teilen den Arbeitsordner. Die Anweisung des führenden
  Agenten bittet ihn, die Arbeit aufzuteilen, aber nichts hindert zwei
  Agenten daran, dieselbe Datei zu ändern.
- Das Brett weiß nicht, wer es aufruft: Ein Mitglied nennt sich selbst. Die
  Mitglieder sind Ihre eigenen Agenten, aber eines könnte die Aufgabe eines
  anderen nehmen.
- Die erste Anweisung an OpenCode geht über `--prompt`; das lief hier noch
  nicht.

## Andere Hosts

`sandbox-ai hosts` verwaltet andere Rechner (oder Proxmox-LXC-Container, siehe
[PROXMOX_LXC_DE.md](PROXMOX_LXC_DE.md)), deren sandbox-ai dieses über SSH
steuert. Die Desktop-App und die Web-Oberfläche nutzen dieselbe Liste.

```bash
sandbox-ai hosts add ct1 user@ct1.lan --description "Proxmox 101"
sandbox-ai hosts test ct1                 # SSH, seine sandbox-ai-Version, sein doctor
sandbox-ai hosts run ct1 -- list          # sandbox-ai list auf ct1
sandbox-ai hosts run ct1 -- attach web    # in eine Sitzung dort (ein Terminal über SSH)
sandbox-ai hosts list                     # die sandbox-ai-Version jedes Hosts und ob ein Update da ist
sandbox-ai hosts install ct1              # sandbox-ai auf ct1 (die Kopie dieser Maschine; --download: das signierte Release)
sandbox-ai hosts install ct1 --system     # für alle Benutzer dort: als root über SSH, sonst mit sudo
sandbox-ai hosts update ct1               # sandbox-ai update auf ct1: alte -> neue Version
sandbox-ai hosts update --all             # jeder Host der Liste, einer nach dem anderen, und eine Zusammenfassung
sandbox-ai hosts remove ct1
```

Die Liste liegt in `~/.config/sandbox-ai/hosts.json`, neben `local` (diesem
Rechner). Jeder Host wird mit Ihren SSH-Schlüsseln erreicht:

- `BatchMode`: nie eine Passwortabfrage, die hängen bleibt;
- der Host-Schlüssel wird beim ersten Kontakt gemerkt (in
  `~/.config/sandbox-ai/known_hosts`, `~/.ssh/known_hosts` wird auch gelesen)
  und danach streng geprüft;
- jedes Argument wird einzeln gequotet;
- `--identity DATEI` wählt einen Schlüssel, `--port N` einen Port.

ssh lehnt eine Schlüsseldatei ab, die andere lesen können ("UNPROTECTED
PRIVATE KEY FILE"). `hosts add` und `hosts test` (und die Hosts-Seiten der
App und der Web-Oberfläche) machen einen solchen Schlüssel privat (`chmod
600`), nennen die Datei und ihren alten Modus und versuchen die Verbindung
erneut: den `--identity` des Hosts, sonst Ihre `~/.ssh/id_*`, und einen
Schlüssel, den ssh als zu offen nennt. Geändert werden nur reguläre Dateien,
die Ihnen gehören, nie über einen symbolischen Link (der wird mit dem Befehl
genannt); ein `~/.ssh`, in das andere schreiben können, wird genannt, nicht
geändert.

Auf den Hosts lauscht nichts ins Netz: Sie brauchen nur sshd und sandbox-ai.
Eine nicht interaktive SSH-Anmeldung liest unter Debian keine `~/.bashrc`,
deshalb nimmt der Befehl ersatzweise `~/.local/bin/sandbox-ai` (wo `install.sh`
es ablegt); `--command PFAD` nennt einen anderen Ort.

### Die Hosts aktualisieren

`sandbox-ai hosts update NAME...` (oder `--all`) führt auf jedem Host dessen
eigenes `sandbox-ai update` aus: das Update von install.sh, geprüft gegen die
Release-Schlüssel der Installation dort, als dieselbe Art Installation. Es
zeigt pro Host die alte und die neue Version, was schiefging, und eine
Zusammenfassung; `--version X.Y.Z` wählt ein Release, `--jobs N` aktualisiert
N Hosts gleichzeitig, `--json` ist für Programme. `hosts list` zeigt die
Version jedes Hosts und ob ein neueres Release da ist (das eigene `update
--check` des Hosts, 10 Minuten gemerkt; `--refresh` fragt sofort, `--cached`
fragt keinen Host).

| Installation auf dem Host | Was `hosts update` tut |
|---|---|
| install.sh für einen Benutzer (`~/.local/share/sandbox-ai`) | `sandbox-ai update --user` als dieser Benutzer |
| install.sh als root (`PREFIX/lib/sandbox-ai`) | `sudo -n sandbox-ai update --system --prefix PREFIX` mit `--sudo` oder der Einstellung des Hosts (`hosts set NAME --update-sudo on`, oder `hosts add ... --update-sudo`); ohne sie sagt es das |
| ein Checkout, pip, ein Paket (pyz, AppImage) | nichts: es nennt die Installation und den Weg, sie zu aktualisieren (git, pip, die Release-Seite), oder sie mit install.sh zu installieren |

`sudo -n` fragt nie nach einem Passwort. Will sudo dort eines, sagt `hosts
update` das und führt das Update aus einem Terminal heraus dort mit `ssh -t
... sudo` aus, wo sudo fragt; die App und die Web-Oberfläche öffnen es in
einem Terminal-Tab. Eine Regel in sudoers (`NOPASSWD` für genau diesen
Befehl) erspart die Frage. Dieser Rechner aktualisiert sich mit `sandbox-ai
update` (`--all` lässt ihn aus).

## Projektkonfiguration und Wizard

```bash
./sandbox-ai wizard                  # interaktive Einrichtung, schreibt .sandbox-ai.json
./sandbox-ai wizard edit [VERZ]      # vorhandene Konfiguration eines Projekts ändern
./sandbox-ai wizard trust DATEI      # Konfiguration prüfen und vertrauen
./sandbox-ai wizard forget DATEI     # aus der Vertrauensliste entfernen
```

Der Wizard fragt Projektverzeichnis, den [Sicherheitsmodus](#sicherheitsmodi)
(Enter behält den geerbten: dann wird nichts geschrieben; spätere Fragen bieten
nur an, was der Modus erlaubt), weitere Workspaces, Agent, Backend, Netz
(oder einen mit `sandbox-ai llm` angelegten LLM-Server, für jeden Agenten; für
OpenCode auch direkt einen lokalen Server), Modus ohne Rückfragen, GUI, tmux und
Logs ab. Das Netz ist `default` (nur der Anbieter des Agenten), `development`
(dazu PyPI, GitHub und die APIs von OpenAI und Anthropic), `frontier`, `dns`,
`egress-all` oder `offline`; außer bei offline setzt danach eine erweiterte
Frage (Standard nein) die eigenen Adressen der Sandbox (`--subnet`, mit Podman
`--subnet6`), beim Eintippen gegen die Routen dieses Hosts geprüft. Antworten
lassen sich wie in einer Shell
bearbeiten (Backspace, Pfeiltasten, Ctrl-A/E/U, Verlauf mit Pfeil hoch), Tab
ergänzt Pfade. Danach folgen drei optionale Blöcke, jeweils hinter einer
Ja/Nein-Frage:

- **Weitere Netzziele**: Vorlagen (`--allow-github`, ...), `--allow-host`,
  `--allow-tcp`, `--allow` und mit Podman `--allow-host-port` (ein Dienst auf
  diesem Host, etwa ein lokales Gitea).
- **Git**: `--git-identity` und Push-Tokens für github.com und eigene Server
  (etwa ein lokales Gitea). Den Token eingeben (verdeckt; er landet in einer
  privaten Datei unter `~/.config/sandbox-ai/tokens/`) oder eine Host-Variable
  nennen (Vorgabe `SANDBOX_GH_TOKEN`, bzw. `GH_TOKEN`/`GITHUB_TOKEN`, falls
  gesetzt). Einen Token für das `origin`-Remote des Projekts, gelesen
  als Text aus `.git/config`, ohne git auszuführen, bietet er nur nach
  ausdrücklichem Ja an (Vorgabe Nein): Der Agent kann diese Datei ändern.
  Gespeichert werden nur Variablennamen, nie Tokens; noch nicht gesetzte
  Variablen meldet der Wizard.
- **Weitere Optionen**: `--workspace-ro`, `--state-dir`, `--mount`, `--env`,
  `--mcp`, `--gui-screen`, `--gui-vnc` und ein freies Feld für jede andere
  Starter-Option, geprüft vom Parser des Starters.

**Bearbeiten.** `sandbox-ai wizard edit [VERZ|DATEI]` (oder `new` in einem Projekt
mit Konfiguration, Antwort `edit`) lädt die vorhandene `.sandbox-ai.json` und
zeigt ihre Teile als Menü: Agent, Backend, Workspaces, Netz, Verhalten
(Rückfragen, Display, tmux, Logs), weitere Netzziele, Git, weitere Optionen,
Agent-Argumente und für Fortgeschrittene alle Launcher-Optionen als eine Zeile.
Jeder Teil stellt seine Fragen erneut, mit den aktuellen Werten als Vorgabe:
Enter behält einen Wert, `-` leert eine Liste, und im Terminal steht eine
Optionszeile fertig zum Bearbeiten da. Vorhandene Push-Tokens lassen sich
einzeln behalten; Optionen, nach denen der Wizard nicht fragt, bleiben in der
freien Optionszeile; Beschreibung, Agent-Argumente und MCP-Definitionen bleiben
erhalten. Vor dem Schreiben zeigt der Wizard die neue Datei und was sich ändert
(`- alte Option`, `+ neue Option`). Eine nicht vertraute Datei (neu oder seitdem
geändert: Der Agent kann sie schreiben) wird zuerst vollständig gezeigt und nur
nach ausdrücklichem Ja verwendet; beim Schreiben wird genau das geprüfte Ergebnis
vertraut.

Jede Eingabe wird sofort geprüft, die fertige Konfiguration zusätzlich mit
den Prüfungen des Starters; würde er den Start verweigern (etwa weil eine
Token-Variable noch nicht exportiert ist), sagt der Wizard vor dem Schreiben
warum. Danach zeigt er das Ergebnis und schreibt `.sandbox-ai.json` ins
Projekt. Für ein
lokales LLM landet die OpenCode-Konfiguration unter
`~/.config/sandbox-ai/opencode/`, außerhalb des Workspace: Der Starter lehnt
Quellen für `--local-config`, `--copy`, `--auth-file` und `--mount` ab, die
der Agent durch einen Symlink ersetzen könnte.

Ein Starter liest `.sandbox-ai.json` danach automatisch aus dem
Haupt-Workspace; `--config DATEI` wählt eine andere Datei, `--no-config`
ignoriert sie. **Der Ordner, in dem Sie starten,
entscheidet:** Liegt dort eine `.sandbox-ai.json`, ist er das Projekt – der Haupt-Workspace (`/work`)
und die Datei, die gilt –, und was `--workspace` nennt, folgt als weitere Workspaces (`/workspaces/NAME`);
ohne Projektdatei dort ist wie bisher der erste `--workspace` der Haupt-Workspace. Ein `--workspace`, der
diesen Ordner noch einmal nennt (`.`, `$PWD/`), zählt einmal; einer darin oder darüber (`--workspace
packages/web` aus der Wurzel eines Monorepos) ist der Ordner, den Sie gewählt haben, und Ihre Reihenfolge gilt.
Jeder Start nennt, welcher Ordner `/work` ist und welche Konfiguration gilt. (Starts, die sandbox-ai selbst
baut – aus der App, der Weboberfläche, `team start`, `resume` und `restart` –, nehmen ihre Workspaces, wie sie
sind: Eine Projektdatei, die später in ihrem Startordner auftaucht, übernimmt sie nie.) Optionen mit Wert auf der Kommandozeile haben Vorrang vor denen
der Datei, eine Auth-Wahl (`--copy-auth`, `--no-auth`, `--auth-file`,
`--import-codex-auth`, ein `--copy` einer Anmeldedatei) ersetzt die der Datei, und Agent-Argumente nach `--`
ersetzen die gespeicherten; einen Schalter, den die Datei setzt (z. B.
`--offline`), kann die Kommandozeile nicht aufheben, dafür `--no-config`
verwenden. **Eine Projektdatei wirkt
erst, wenn Sie ihr vertrauen:** Ein geklontes Repository könnte die Sandbox
sonst erweitern (`--egress-all-allow`, `--mount ~/.ssh:…`). Der SHA-256 jeder
vertrauten Datei liegt in `~/.config/sandbox-ai/trusted.json`; der Wizard
vertraut, was er schreibt, und jede Änderung verlangt erneut
`sandbox-ai wizard trust`, das genau die angezeigten Bytes vertraut. Die Datei
wird aus einem einzigen Lesevorgang gehasht und ausgewertet, muss eine reguläre
Datei sein und wird als Symlink abgelehnt. Das Löschen einer vertrauten Datei
hebt ihre Einschränkungen nicht still auf: Der Starter bricht mit einem Fehler
ab, bis sie wiederhergestellt ist, `--no-config` angegeben wird oder
`sandbox-ai wizard forget DATEI` sie aus der Vertrauensliste entfernt. Die Datei
darf weder `--workspace` (der Haupt-Workspace ist der Ort der Datei) noch
`--session`, `--config` oder interne Optionen setzen, und Optionsnamen müssen
ausgeschrieben sein (keine Abkürzungen). Pfade (`workspaces`, `--mount`,
`--state-dir`, Token-Dateien ...) müssen absolut sein oder mit `~` beginnen,
damit die Datei überall dasselbe bedeutet, egal wo der Starter aufgerufen wird.
Der Schlüssel `"rules"` enthält ein [Regelwerk](#regelwerke), das mit der Datei
gehasht wird; es kann nur einschränken, was Ihre eigenen Regelwerke erlauben.
`--rules` lässt sich in einer Projektdatei nicht setzen: Eine Regeldatei an
anderer Stelle wäre vom Hash nicht erfasst. `"log_debug": true` wirkt wie
`--log-debug`. `"mode": "high"` wählt den [Sicherheitsmodus](#sicherheitsmodi)
des Projekts (`--mode` auf der Kommandozeile hat Vorrang). `"name": "shop"` ist der PROJEKT-Teil der automatischen Namen
(`claude-shop-1a2b`; Vorgabe: der Ordnername des Haupt-Workspace; `--project-name`
auf der Befehlszeile hat Vorrang); `--name` und `--project-name` sind in
`options` nicht erlaubt. `"keep": false` verwirft das HOME der Starts des
Projekts am Ende (`--no-keep`; `--keep` auf der Befehlszeile hat Vorrang).

```json
{
  "version": 1,
  "agent": "opencode",
  "options": ["--local-config", "/home/ich/.config/sandbox-ai/opencode/app-1a2b3c4d5e6f.json",
              "--allow-tcp", "192.168.1.20:11434", "--no-tmux"],
  "workspaces": ["/home/ich/src/shared-lib"],
  "agent_args": []
}
```

## Jeder Start: Namen, lösen, aufbewahren, fortsetzen

Jeder Start eines Agenten hat einen Namen, erscheint in `sandbox-ai list` und
lässt sich über diesen Namen verwalten. Sein HOME wird aufbewahrt, sodass ein
gestoppter Start später zurückkommen kann: eine neue Sandbox mit denselben
Optionen und demselben HOME, in der der Agent sein letztes Gespräch fortsetzt.

```bash
sandbox-ai claude -w ~/src/app                    # claude-app-3f9a: in tmux, dieses Terminal verbunden
# Ctrl-b d oder das Schließen des Terminals löst; der Agent arbeitet weiter.
sandbox-ai list                                    # was läuft oder pausiert, und die aufbewahrten Starts
sandbox-ai attach claude-app-3f9a                  # wieder hinein
sandbox-ai rename claude-app-3f9a app              # ein anderer Name; die Run-ID bleibt
sandbox-ai pause app                               # jeden Prozess einfrieren
sandbox-ai resume app                              # wieder auftauen
sandbox-ai stop app                                # beenden; HOME, Optionen und Logs bleiben
sandbox-ai resume app                              # später: wiedergeboren, der Agent setzt sein Gespräch fort
sandbox-ai resume app --restart -w ~/src/lib       # beendet und neu gestartet, mit /workspaces/lib dazu (bleibt)
sandbox-ai list --history app                      # Start, Ende, Pause, Fortsetzen, Stopp, Wiedergeburt, Umbenennung
sandbox-ai delete app --yes                        # samt HOME entfernen
sandbox-ai gc --older-than 30 --dry-run            # aufbewahrte Starts, 30 Tage unbenutzt: was ginge
sandbox-ai claude --no-keep -- -p "review the diff"   # für Skripte: das HOME geht mit dem Ende
```

**Namen.** Ein Start heißt `AGENT-PROJEKT-xxxx` (`claude-app-3f9a`): PROJEKT
ist `--project-name`, sonst `"name"` aus der Projektdatei, sonst der
Ordnername des Haupt-Workspace, passend gemacht; `xxxx` ist zufällig.
`--name NAME` wählt den ganzen Namen. Namen haben 1-40 Buchstaben, Ziffern,
`_`, `.` oder `-` und beginnen mit Buchstabe oder Ziffer; `mcp-…` gehört den
MCP-Server-Sandboxen. Ein Name, unter dem ein Start läuft, pausiert oder
aufbewahrt ist, wird mit Hinweis abgelehnt (`sandbox-ai resume NAME`,
`delete` oder ein anderer Name). `sandbox-ai rename ALT NEU` ändert nur den
Namen: Die Run-ID (`$SANDBOX_AI_RUN` der Sandbox, ihr Steuer-Socket,
Verbindungslogs, Freigaben und Aktivität) bleibt. Jeder Befehl nimmt den Namen
oder die Run-ID, und alles, was einen Run zeigt, zeigt seinen Namen
(`net list`, Freigaben, `activity`).

**Interaktive Starts sind Sessions.** Ein Start aus einem Terminal, der die
eigene Oberfläche des Agenten öffnet (kein Prompt, kein Unterbefehl), läuft
als lösbare Session ([tmux-Modus](#tmux-modus)): Ein Supervisor im Hintergrund
betreibt die Sandbox, Ihr Terminal verbindet sich mit tmux darin (Fenster
`agent`, Fenster `shell`). Ctrl-b d oder das Schließen des Terminals löst die
Verbindung; der Agent arbeitet weiter, `sandbox-ai attach NAME` holt das
Terminal zurück. Endet der Agent, kehrt der Befehl mit seinem Exit-Status
zurück.

**Aufgaben bleiben im Vordergrund.** `-p`, `codex exec`, `opencode run`, ein
Start ohne Terminal (Pipe, Skript), `--no-tmux`, `--probe` und `--dry-run`
laufen wie bisher im Vordergrund, mit Ausgabe und Exit-Status. Sie werden
gelistet, solange sie laufen, lassen sich per Name pausieren, fortsetzen und
stoppen und haben einen Verlauf; verbinden lassen sie sich nicht. Auch ihr HOME
bleibt (nicht bei Probes und Plänen), sodass `sandbox-ai resume NAME` das Gespräch einer solchen Aufgabe
interaktiv fortsetzen kann.

**Das HOME bleibt.** Standardmäßig bewahrt ein Start beim Ende sein HOME,
seine Optionen und Logs in `~/.local/state/sandbox-ai/sessions/ID/` auf:
`home/` (das `/root` der Sandbox: Gespräche, Einstellungen, Logins, dort
Installiertes), `session.json` (Agent, Optionen, Agent-Argumente, Name, Run),
`supervisor.log` (Ausgabe des Starters einer Session), `exit.json` (Exit-Status
des letzten Laufs), `home-manifest.json` (siehe unten). `--no-keep` (oder
`"keep": false` in der Projektdatei) verwirft das HOME am Ende, wie es jeder
Start vor 0.1.30 tat: Ein frisches HOME bei jedem Start heißt, dass ein
kompromittierter Agent nichts für den nächsten Start hinterlassen kann. Das
passt zu Skripten und Einmal-Aufgaben. `--keep` macht den Standard ausdrücklich
(und übersteuert `"keep": false` der Projektdatei). `--state-dir DIR` ist ein
eigenes HOME: Es bleibt ohnehin, der Start bewahrt kein zweites auf. Probes,
Pläne und MCP-Server bewahren nichts auf. Der
[Sicherheitsmodus](#sicherheitsmodi) `paranoid` bewahrt gar kein HOME auf:
Jeder Start verwirft es, `--keep` und `--session` werden abgelehnt, und ein
aufbewahrtes HOME wird darunter nicht zurückgeholt; `high` bewahrt HOMEs auf.
Ein aufbewahrtes HOME behält seine [Köder-Dateien](#köderdateien-canaries) von
Lauf zu Lauf (dieselben Dateien und Tokens).

Aufbewahrte Starts verfallen nie von selbst. `sandbox-ai gc` entfernt Reste
(Sessions, die ohne aufbewahrtes HOME endeten), `sandbox-ai gc
--older-than TAGE` zusätzlich die gestoppten aufbewahrten Starts, die so lange
unbenutzt sind, und die Verläufe verschwundener Namen; `--dry-run` listet nur,
`--json` für Programme. Mit `"keep_days": N` in
`~/.config/sandbox-ai/defaults.json` verfallen aufbewahrte Starts: Jeder neue
Start entfernt die gestoppten, die länger als N Tage unbenutzt sind (und sagt
es), und `sandbox-ai gc` nimmt N, wenn kein `--older-than` angegeben ist.

**Wiedergeburt.** `sandbox-ai resume NAME` taut einen pausierten Start auf; bei
einem gestoppten, aufbewahrten Start entsteht eine neue Sandbox mit denselben
Optionen und demselben HOME (eine *Wiedergeburt* mit neuer Run-ID, im Verlauf
mit dem Namen verknüpft), und der Agent setzt sein letztes Gespräch fort:
`claude --continue`, `codex resume --last`, `opencode --continue` (jeweils auf
`/work` beschränkt, bei jedem Start dasselbe). Die ursprünglichen
Agent-Optionen (von der Befehlszeile, sonst `agent_args` der Projektdatei, etwa
`--model sonnet`) bleiben bis zum ersten Prompt davor erhalten; Prompts,
Einmal-Aufgaben (`-p`, `exec`, `run`) und Gesprächswahlen (`--resume`) werden
nicht wiederholt. Mit `--binary` werden die ursprünglichen Argumente wieder
verwendet. Die `.sandbox-ai.json` des Projekts wird bei jedem Start neu
gelesen. `--session NAME` (die ältere Form) startet eine aufbewahrte, benannte
Session oder holt eine gestoppte dieses Namens zurück; dabei angegebene
Optionen gelten nicht (der Starter warnt und nennt sie). **Laufende Prozesse
werden nicht gesichert:** Es überleben `home/` und die Workspaces. Prozesse,
`/tmp`, der tmux-Verlauf und anderswo im Container geschriebene Dateien sind
weg; einen Dev-Server oder Build, den der Agent gestartet hatte, muss man neu
starten. „Letztes Gespräch" ist das jüngste in diesem HOME; eines, das von Hand
im Fenster `shell` begonnen wurde, hat also Vorrang.

**Was sich im HOME geändert hat.** Ein aufbewahrtes HOME ist das Einzige, womit
ein kompromittierter Agent seinen nächsten Start erreichen könnte: eine Zeile
in `.bashrc`, ein Hook in `.claude/settings.json`, ein Plugin oder Skill, ein
MCP-Server in `~/.claude.json`, ein Git-Alias, eine SSH-Konfiguration, eine
`.pth`-Datei, die Python ausführt. Darum hält der Starter bei jedem Start eines
aufbewahrten HOME ein Manifest dieser Dateien fest (Hashes, Rechte, Linkziele
und den Text kleiner Dateien ohne Schlüssel oder Tokens), im Verzeichnis des
Starts außerhalb des HOME, wo die Sandbox es nicht erreicht. Bei der
Wiedergeburt wird das HOME damit verglichen, und jede Änderung seit dem letzten
Start wird genannt, bevor die Sandbox startet; im Terminal bestätigen Sie mit
Enter (oder brechen mit `n` ab). `sandbox-ai resume NAME --review-home` zeigt
stattdessen die Unterschiede (`--json` für die App: `changes` mit `path`,
`change`: added, removed, changed, mode oder type, und `diff`). Änderungen, die
Sie selbst gemacht haben oder die der Agent auf Ihren Wunsch gemacht hat (eine
Einstellung, ein Plugin), sind zu erwarten; alles andere ist ein Grund,
nachzusehen, bevor es weitergeht. Schlüssel und Tokens (`~/.ssh/id_*`,
`.git-credentials`, `.netrc`, ...) werden nur per Hash verglichen und nie
gezeigt.

**Pausieren und stoppen.** `pause` friert den ganzen Podman-Pod ein
(cgroup-Freezer) oder schickt mit Bubblewrap SIGSTOP an jeden Prozess des
Sandbox-Baums (bei einer Aufgabe im Vordergrund: unterhalb ihres Starters, der
Ihr Terminal behält); `resume` taut ihn auf. `stop` schickt SIGTERM an den
Supervisor (oder den Starter der Aufgabe), der normal aufräumt, einschließlich
des Rückschreibens der Claude-Anmeldedaten; MCP-Server-Sandboxen (`mcp-NAME` in
der Liste) werden ebenso gestoppt. Befehle, die einen Start ändern, halten eine
Sperre; ein zweiter gleichzeitiger scheitert mit einer Meldung, statt doppelt
zu starten. Nach einem Absturz, `kill -9` oder Neustart konnte der Supervisor
seinen Podman-Pod nicht entfernen; die nächste Wiedergeburt entfernt ihn, aber
nur, wenn der Pod das Label dieses Starts trägt (`sandbox-ai.session`); jeden
anderen Pod dieses Namens lässt sie in Ruhe, und die Meldung nennt den Befehl
`podman pod rm --force`.

**Verlauf.** Jeder Name hat einen kleinen Verlauf in
`~/.local/state/sandbox-ai/history/NAME.jsonl` (Start, Ende mit Status, Pause,
Fortsetzen, Stopp, Wiedergeburt, Umbenennung, Löschen; jeweils Zeit und
Run-ID), gezeigt von `sandbox-ai list --history NAME` (`--json` für Programme).
Beim Umbenennen wandern die Einträge des Starts zum neuen Namen.
`sandbox-ai list --all` zeigt zusätzlich die MCP-Server, die nicht laufen.

**Die älteren Formen** bleiben: `--session NAME` und `sandbox-ai session
list|attach|pause|resume|stop|delete` wirken wie bisher, auf dieselben Starts
(der Einzelbefehl `sandbox-session` ist veraltet).

Beim Verbinden läuft nie ein tmux-Client auf dem Host: Der tmux-Server gehört
dem Agenten und könnte einem verbundenen Client Befehle auftragen.
Stattdessen startet ein Relay in der Sandbox, hinter Firejail, `tmux attach`
auf einem eigenen Pseudo-Terminal, und `sandbox-ai attach` tauscht mit ihm über
`tmux/attach.sock` nur Terminal-Bytes und die Fenstergröße aus. `--session`
lässt sich nicht mit `--state-dir` oder `--probe` kombinieren.

## tmux-Modus

tmux ist der Standard, und damit ist jeder interaktive Start lösbar: Sind
stdin und stdout ein Terminal, läuft der Start als
[Session](#jeder-start-namen-lösen-aufbewahren-fortsetzen). Ein Supervisor im
Hintergrund betreibt die Sandbox, der Agent läuft darin in tmux mit den
Fenstern `agent` und `shell` (beide hinter derselben Firejail und Firewall),
und Ihr Terminal verbindet sich. **Ctrl-b d oder das Schließen des Terminals
löst die Verbindung**; der Agent arbeitet weiter, `sandbox-ai attach NAME` holt
ihn zurück. Endet der Agent, bleibt die Shell; mit dem letzten Fenster endet die
Sandbox, und der Befehl kehrt mit dem Exit-Status des Agenten zurück. Der Name
des Starts (`AGENT-PROJEKT-xxxx` oder `--name`) wird beim Start ausgegeben und
von `sandbox-ai list` gezeigt.

tmux wird nur genutzt, wenn die Agent-Argumente reine Optionen sind (`claude`,
`claude --model opus`); ein Unterbefehl, Prompt oder eine
Ausgabe-und-Ende-Option (`claude -p`, `claude mcp list`, `codex exec`,
`opencode run`, `--version`) läuft im Vordergrund ohne tmux, damit Ausgabe und
Exit-Status sichtbar bleiben. Aus bleibt es auch bei Probes, MCP-Servern,
`--binary` und mit `--backend bwrap`, wenn der Host kein tmux hat. Solche Starts
werden verfolgt (gelistet, solange sie laufen, per Name pausiert und gestoppt,
mit Verlauf), lassen sich aber nicht verbinden. `--no-tmux` lässt den Agenten
ebenfalls im Vordergrund laufen; `--tmux` besteht auf tmux (und scheitert ohne
Terminal).

## GUI-Programme und Browser

| Option | Wirkung |
|---|---|
| `-gu`, `--gui` | Virtuelles X-Display `:99` (Xvfb); `DISPLAY` gesetzt, `/dev/shm` für Chromium vergrößert. |
| `-gs`, `--gui-screen BxHxT` | Bildschirmgröße, Standard `1920x1080x24`. |
| `-gv`, `--gui-vnc VERZ` | Impliziert `--gui`; VNC auf dem Unix-Socket `VERZ/vnc.sock` (Modus `0700`, kein TCP). |
| `-gw`, `--gui-wayland` | Wayland-Socket des Hosts freigeben: Fenster erscheinen auf Ihrem Desktop. |

Xvfb und x11vnc laufen im Supervisor ohne jede Capability; das VNC-Verzeichnis
ist für den Agenten ausgeblendet und muss außerhalb der Workspaces liegen. Das
Display mit einem Client betrachten, der Unix-Sockets kann, z. B.
`vncviewer VERZ/vnc.sock` (TigerVNC), oder
`socat TCP-LISTEN:5901,bind=127.0.0.1 UNIX-CONNECT:VERZ/vnc.sock` plus
beliebigem Viewer auf `127.0.0.1:5901`. `--gui-wayland` gibt dem Agenten
Zugriff auf Ihren Compositor und gibt eine Warnung aus; X11-Durchreichung gibt
es bewusst nicht, weil X11 Tastatureingaben und Bildschirminhalte aller
Host-Fenster offenlegt.

Browser sind nicht im Image; Playwright lädt sie (Netz nötig):

```bash
./sandbox-ai codex --workspace "$HOME/src/app" --gui --egress-all-allow \
  --state-dir "$HOME/.local/state/sandbox-ai/pw" --binary /bin/bash
# darin: uvx --from playwright playwright install chromium
#        uv run --with playwright python mein_browsertest.py   # headless=False funktioniert
```

**Ton** (`--audio virtual`): ein PulseAudio-Server in der Sandbox mit einem
Null-Sink: Was Programme abspielen, lässt sich von seinem Monitor aufnehmen
(Sprachausgabe, Bildschirmvideos mit Ton, `ffmpeg -f pulse -i sandbox.monitor`),
und nichts erreicht Ihre Soundkarte oder Ihre Mikrofone. Er läuft als eigener
Benutzer ohne Capabilities; die Programme des Agenten finden ihn über
`PULSE_SERVER`. Das Image braucht PulseAudio: `sandbox-ai images recipe media
--from claude` legt die Variante `media` an (ffmpeg, PulseAudio, SoX, espeak-ng,
ImageMagick, scrot, Piper), `sandbox-ai build media` baut sie,
`--image-variant media` startet sie. Ohne PulseAudio im Image sagt der Start das
und läuft ohne Ton weiter. Abspielen über Ihre eigenen Lautsprecher gibt es
nicht: Ihr Soundserver gäbe der Sandbox auch Ihre Mikrofone.

## Hardware: Geräte und GPUs

Ohne weitere Optionen sieht eine Sandbox keine Hardware des Hosts: Podman und
Bubblewrap geben ihr ein eigenes, minimales `/dev`, und `--mount` lehnt
Gerätedateien ab. Es gibt drei Wege, dem Agenten Hardware zu geben, der
sicherste zuerst:

| Weg | Wie | Isolation | Funktioniert ohne Vorarbeit? |
|---|---|---|---|
| 1. Dienst auf dem Host | Das Gerät bleibt auf dem Host; ein Programm dort bietet es über TCP an, die Sandbox darf diesen Port erreichen | Kein Treiber des Hosts erreichbar, Ab- und Anstecken stört nicht | Sie starten und richten den Dienst ein |
| 2. `--device PFAD` | Die Gerätedatei selbst | Der Agent spricht mit dem Treiber des Geräts im Kernel Ihres Hosts | Oft (siehe Tabelle unten) |
| 3. `--gpu nvidia\|dri\|rocm` | Alle GPU-Geräte mit einer Option | Wie `--device` | Ja für NVIDIA mit CDI; Intel/AMD brauchen GPU-Bibliotheken im Image |

`sandbox-ai claude --help` fasst das am Ende zusammen.

### 1. Ein Dienst auf dem Host (am sichersten)

Der Agent bekommt einen Netzwerkport, kein Gerät. Mit Podman sorgt
`--allow-host-port PORT` dafür, dass `127.0.0.1:PORT` in der Sandbox denselben
Port auf dem Loopback des Hosts erreicht; mit Bubblewrap
`--allow-tcp HOST_IP:PORT`.

| Hardware | Auf dem Host | In der Sandbox |
|---|---|---|
| Serielle Schnittstelle (Mikrocontroller) | `ser2net` mit RFC 2217 oder `socat TCP-LISTEN:4000,bind=127.0.0.1,fork,reuseaddr FILE:/dev/ttyACM0,raw,echo=0` | `--allow-host-port 4000`; esptool und pyserial sprechen `rfc2217://127.0.0.1:4000` (ser2net) oder `socket://127.0.0.1:4000` (socat) |
| Debug-Probe (JTAG/SWD) | `openocd` (GDB-Port 3333) oder ein GDB-Server | `--allow-host-port 3333`; `target extended-remote 127.0.0.1:3333` |
| Android-Gerät | `adb start-server` (Port 5037) | `--allow-host-port 5037`; `adb -H 127.0.0.1 -P 5037 ...` |
| GPU für Modelle | Ollama, llama.cpp oder vLLM | `--llm NAME` oder `--allow-host-port 11434` o. ä., siehe [LLM-Server](#llm-server-lokal-und-entfernt) |

Nur das Protokoll dieses Dienstes erreicht den Host. Das Gerät kann beliebig
ab- und angesteckt werden, weil der Dienst es öffnet, nicht die Sandbox.

### 2. `--device PFAD`

```bash
sandbox-ai claude --device /dev/serial/by-id/usb-Raspberry_Pi_Pico_E660-if00
sandbox-ai claude --device usb:2e8a:000a       # dasselbe Board über seine USB-IDs, egal unter welchem Knoten
sandbox-ai claude --device /dev/kvm            # Emulatoren, QEMU
sandbox-ai claude --device /dev/bus/usb        # USB direkt, später angesteckte Geräte erscheinen mit
```

- Angenommen werden zeichenorientierte Geräte unter `/dev` und die
  Verzeichnisse `/dev/bus/usb`, `/dev/dri` und `/dev/snd` (oder eines darunter).
  Symlinks wie `/dev/serial/by-id/...` werden aufgelöst; der Agent sieht den
  echten Namen (`/dev/ttyACM0`).
- Immer abgelehnt: Block-Geräte (Festplatten), `/dev/mem`, `/dev/kmem`,
  `/dev/port`, Konsolen (`/dev/console`, `/dev/tty`, `/dev/tty1`, `/dev/vcs*`),
  Eingabegeräte (`/dev/input/*`, `/dev/uinput`, `/dev/hidraw*`, darunter auch
  Security-Keys), direkter Plattenzugriff (`/dev/sg*`, `/dev/nvme0`,
  `/dev/vfio`, `/dev/mapper/control`, `/dev/loop-control`), Interna des Hosts
  (`/dev/kmsg`, Watchdog, TPM, Intel ME, `/dev/cpu`, MTD-Flash) und Kanäle an
  der Firewall vorbei (`/dev/vsock`, `/dev/vhost-*`).
- Kameras (`/dev/video*`) und Sound (`/dev/snd`) werden mit Warnung
  durchgereicht: Der Agent kann Bilder aufnehmen und über Ihre Mikrofone
  mithören.
- Der Start listet jedes Gerät auf. Mit Podman werden einzelne Geräte zu
  `--device`, Verzeichnisse zu Bind-Mounts; mit Bubblewrap beides zu
  `--dev-bind`.

**Zugriffsrechte.** Die Sandbox behält keine Ihrer Zusatzgruppen (Firejail
verwirft sie). Der Agent kann ein Gerät nur öffnen, wenn Ihr Benutzer es als
Eigentümer, über seine primäre Gruppe, als „alle“ oder über einen ACL-Eintrag
darf; letzteren setzt eine Desktop-Sitzung für Geräte, die udev mit `uaccess`
markiert. Der Launcher prüft das und warnt sonst.

| Gerät | Übliche Rechte | Ohne Vorarbeit? |
|---|---|---|
| `/dev/kvm` | `0666` (systemd) | Ja |
| `/dev/dri/renderD*`, `/dev/kfd` | `0666` (systemd, `kfd` in neueren Versionen) | Ja |
| `/dev/nvidia*` | `0666` (NVIDIA-Treiber) | Ja |
| `/dev/video*`, `/dev/snd/*` | ACL für den Benutzer am Desktop (`uaccess`) | Am Desktop, nicht über SSH |
| `/dev/ttyUSB*`, `/dev/ttyACM*` | Gruppe `dialout` oder `uucp` | Nein: udev-Regel |
| `/dev/bus/usb/...` | `0664` root, ACL für manche Geräte | Hängt von den udev-Regeln des Geräts ab |

Eine udev-Regel gibt Ihrem Benutzer ein einzelnes Gerät (die IDs zeigt `lsusb`):

```bash
# /etc/udev/rules.d/70-sandbox-ai.rules (vor 73-seat-late.rules, das uaccess anwendet)
SUBSYSTEM=="tty", ATTRS{idVendor}=="2e8a", ATTRS{idProduct}=="000a", TAG+="uaccess"
SUBSYSTEM=="usb", ATTRS{idVendor}=="0483", ATTRS{idProduct}=="3748", TAG+="uaccess"
# auf einem Server ohne Desktop-Sitzung stattdessen: ..., OWNER="ihrbenutzer"
```

Danach `sudo udevadm control --reload && sudo udevadm trigger` und das Gerät
neu anstecken.

**Über die USB-Identität.** `--device usb:HERSTELLER:PRODUKT[:SERIENNUMMER]`
(die IDs, wie `lsusb` sie zeigt) findet das Gerät über sysfs und übergibt seine
Knoten: seine serielle Schnittstelle, Kamera- oder Sound-Knoten und seinen
USB-Rohknoten (`/dev/bus/usb/BBB/DDD`). Knoten, die `--device` abweist
(`hidraw`, `input`), bleiben weg, ebenso der Rohknoten eines Geräts mit einer
HID-, Massenspeicher-, Smartcard- oder Bluetooth-Schnittstelle: Über rohes USB
könnte der Agent ein solches Gerät seinem Kerneltreiber wegnehmen. Mehrere
Geräte mit denselben IDs werden abgewiesen, wenn keine Seriennummer sie
unterscheidet, ebenso zwei, die dieselbe Seriennummer angeben.

**Während eines Laufs.** `sandbox-ai devices add [LAUF] PFAD|usb:HERSTELLER:PRODUKT[:SERIENNUMMER]`
gibt einer laufenden Sandbox ein Gerät, mit denselben Prüfungen wie `--device`
(abgelehnte Geräte, udev-Regel, Bluetooth, Richtlinie des Administrators, die
[Geräteregeln](#regelwerke)); `devices remove [LAUF] PFAD|usb:...|ID` nimmt ein
hinzugefügtes wieder weg. `devices list [LAUF] [--json]` zeigt jedes Gerät des
Laufs, beim Start übergeben oder hinzugefügt: seine Knoten, was es ist (IDs,
Produktname, die Seriennummer als kurzer Hash, USB-Port), die Regel, die
entschieden hat, und seinen Zustand (`attached`, `held` für eine Rückfrage,
`gone`, `dropped`, `refused`, `removed`). In der Sandbox läuft dafür nichts:
Ein Helfer Ihres Benutzers betritt den User-Namespace der Sandbox, der Ihnen
gehört, und hängt den Knoten in jeden Mount-Namespace der Sandbox ein, den des
Agenten unter Firejail eingeschlossen (ab Linux 5.2). Beim Start übergebene
Geräte bleiben.

**Ab- und Anstecken.** Solange die Sandbox läuft, beobachtet der Starter jedes
einzelne USB-Gerät, das sie bekommen hat, einmal pro Sekunde über sysfs:

- Abgesteckt, oder ein anderes Gerät hat jetzt seinen Knoten: Seine Knoten
  werden der Sandbox weggenommen (`detach`). Ein dort verbliebener Knoten würde
  das Gerät öffnen, das als nächstes seine Gerätenummer bekommt.
- Über die Identität übergeben (`usb:...`): Kommt ein Gerät mit diesen IDs (und
  dieser Seriennummer) zurück, unter welchem Knoten auch immer (`ttyACM1`, ein
  Bootloader-Modus), und erlauben die Geräteregeln es weiterhin, werden seine
  Knoten wieder eingehängt (`replug`). Bei mehreren Kandidaten wird der am
  selben USB-Port genommen, sonst keiner. Über den Pfad übergeben: nur dasselbe
  Gerät (IDs und Seriennummer) am selben Pfad.
- Länger als 5 Minuten fort: verworfen (`drop`); später angesteckt, geschieht
  nichts mehr.

Verzeichnisse (`/dev/bus/usb`, `/dev/dri`, `/dev/snd`) werden als Ganzes
eingebunden, später angesteckte Geräte erscheinen darin, unbeobachtet.

**Geräteregeln.** Die `"devices"`-Regeln der [Regelwerke](#regelwerke)
beurteilen jedes Gerät beim Start, bei `devices add` und bei jedem
Neuanstecken. Ein deny weist es ab (der Start bricht ab). Ein ask hält es an:
Die Sandbox startet ohne das Gerät, die Rückfrage (Art `device`: seine Knoten,
IDs, Produkt, der Hash der Seriennummer, die Regel) erscheint in `sandbox-ai
approvals`, in der App und als Desktop-Benachrichtigung, und nach der Erlaubnis
wird das Gerät eingehängt. *Allow always* gilt für den Lauf, und mit `run` wird
beim Neuanstecken nicht erneut gefragt. Mit `--ask off` wird ein Start mit einem
Gerät, das eine ask-Regel anhält, abgewiesen; `devices add` wartet im Terminal
auf die Antwort.

**Das Log.** Mit einem Log-Verzeichnis (Standard mit dem Proxy) hält
`devices.jsonl` jede Freigabe, Ablehnung, Rückfrage, Antwort sowie jedes
Einhängen, Aushängen, Neuanstecken, Verwerfen und Entfernen fest, mit IDs,
Produkt, Port, Regel und Regelwerk des Geräts. Seriennummern stehen dort als
kurzer Hash (`sha256:...`; mit `--log-debug` vollständig): Sie können das Gerät
einer Person erkennbar machen. Öffnungen der Knoten zählt inotify auf dem Host,
geschrieben höchstens alle 10 Sekunden je Knoten (`open`: wie oft geöffnet, wie
oft nach dem Schreiben geschlossen). Die Überwachung sitzt am Inode des
Geräteknotens, den der Bind-Mount der Sandbox teilt, und sieht daher Öffnungen
in der Sandbox; sie sieht auch die des Hosts selbst (ModemManager,
udev-Proben), ohne sie unterscheiden zu können, und nichts davon, was ein
Prozess mit einem offen gehaltenen Knoten tut.

**Was USB-IDs wert sind.** USB-IDs, Seriennummern, Produktnamen und die
`/dev/serial/by-id`-Links, die udev daraus macht, sind Angaben des Geräts über
sich selbst. Ein dafür gebautes Gerät kann jede davon vortäuschen: Ein Treffer
identifiziert, er authentifiziert nicht. Der Beobachter unterscheidet ein
Anstecken vom nächsten an dem, was der Kernel vergibt (die Gerätenummer des
Knotens, Bus- und Gerätenummer des USB-Geräts, bei jedem Anstecken neu), und
liest sysfs nur unterhalb von `/sys/devices`, ohne einem Symlink zu folgen, der
anstelle eines Attributs liegt.

**Bluetooth** funktioniert nicht über `--device`: Der Kerneltreiber des Hosts
(`btusb`) belegt den Adapter, sein `/dev/bus/usb`-Knoten nützt dem Agenten also
nichts, und Bluetooth selbst läuft über BlueZ am System-D-Bus des Hosts, den
eine Sandbox nie bekommt. Lassen Sie den Bluetooth-Teil auf dem Host laufen und
den Agenten ihn übers Netz erreichen (`--allow-host-port PORT`). Der Starter
weist darauf hin, wenn ein übergebenes Gerät ein Bluetooth-Adapter ist, und
nennt für ein Gerät, das Ihr Benutzer nicht öffnen kann, die udev-Regel mit
dessen USB-IDs.

**Was es kostet.** Jedes Gerät ist Kernelcode Ihres Hosts, den der Agent an
beiden Sandbox-Schichten vorbei aufrufen kann. USB direkt (`/dev/bus/usb`)
erreicht jedes USB-Gerät, das Ihr Benutzer öffnen darf, auch später
angesteckte. Ein Modem, WLAN-Adapter oder SDR kann außerdem Daten an der
Egress-Firewall vorbei senden. Geben Sie nur, was die Aufgabe braucht, und
bevorzugen Sie Weg 1.

### 3. `--gpu`

| Wert | Podman | Bubblewrap | GPU-Bibliotheken |
|---|---|---|---|
| `nvidia` | CDI-Gerät `nvidia.com/gpu=all`; vorher `sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml` (NVIDIA Container Toolkit) | `/dev/nvidia0...`, `/dev/nvidiactl`, `/dev/nvidia-uvm*`, `/dev/nvidia-modeset` | CDI bringt die Bibliotheken des Treibers und `nvidia-smi` mit; mit Bubblewrap kommen sie aus `/usr` des Hosts |
| `dri` | `/dev/dri` (Render-Knoten von Intel/AMD) | wie Podman | Mesa, Vulkan, VA-API oder OpenCL sind nicht in den Images: eigenes Image darauf bauen und mit `--image` starten |
| `rocm` | `/dev/dri` und `/dev/kfd` | wie Podman | ROCm ebenso im eigenen Image |

```dockerfile
# Containerfile.gpu, gebaut mit: podman build -t localhost/claude-gpu -f Containerfile.gpu .
FROM localhost/claude-sandbox:latest
RUN apt-get update && apt-get install -y --no-install-recommends \
      mesa-vulkan-drivers mesa-opencl-icd vainfo && rm -rf /var/lib/apt/lists/*
```

`sandbox-ai claude --gpu dri --image localhost/claude-gpu`. Mit `--podman-url`
geht nur `--gpu nvidia` (das CDI der Gegenseite); `--device` und
`--gpu dri|rocm` benennen Geräte dieses Hosts.

## Agenten ohne Rückfragen

`--dangerously-skip-permissions` startet den Agenten ohne Freigabedialoge:
Codex erhält `--dangerously-bypass-approvals-and-sandbox`, Claude
`--dangerously-skip-permissions` (plus `IS_SANDBOX=1`, das Claude als root
verlangt), OpenCode `OPENCODE_PERMISSION={"*":"allow"}`. Es entfallen nur die
Rückfragen des Agenten; Podman/Bubblewrap, Firejail und Firewall bleiben.

## Egress- und Dateizugriffs-Logs

`--log-dir VERZ` (Modus `0700`, außerhalb der Workspaces) schreibt JSON-Zeilen
(das `connections-*.jsonl` des Proxys beschreibt
[Verbindungs-Proxy](#verbindungs-proxy); ohne `--log-dir` landen es und das
Egress-Log in `~/.local/state/sandbox-ai/logs/PROJEKT-HASH`, Dateizugriffe
werden dann nicht protokolliert):

- `egress-denied.jsonl`: jedes verworfene Ziel (Adresse, TCP/UDP, Port; erstes
  Auftreten) und jede Auflösung eines nicht erlaubten Namens. Mit Logging gehen
  Anfragen an einen Stub auf `127.0.0.1:53` in der Sandbox, der den Namen
  protokolliert und NXDOMAIN antwortet.
- `processes.jsonl`: jeder Prozess, der in der Sandbox auftaucht, mit PID,
  Elternprozess und Kommandozeile (zweimal pro Sekunde abgefragt, sehr kurze
  Prozesse können fehlen). Geschrieben, sobald ein Log-Verzeichnis benutzt
  wird, auch das Standardverzeichnis. Kommandozeilen werden geschwärzt wie
  das Verbindungs-Log (Token-Formate, Passwörter in URLs, Optionen wie
  `--token`/`--password`) und wörtlich für jede `--env`- und
  `--secret-env`-Variable; die Datei endet bei 256 MiB.
- `files.jsonl`: `read`, `write`, `create`, `delete`, `move-from`, `move-to`
  in den Workspaces, `/root`, `/tmp` und allen Mounts (inotify; Wiederholungen
  je Pfad zwei Sekunden zusammengefasst). inotify liefert keine Prozess-ID;
  Lesezugriffe auf `/usr` und `/etc` werden nicht erfasst. Watches zählen gegen
  `fs.inotify.max_user_watches` des Host-Benutzers; ist das Limit erschöpft,
  wird ein Fehler protokolliert.
- `tools.jsonl`: jeder Aufruf eines eigenen Werkzeugs des Agenten mit seiner
  Entscheidung, wenn Regelwerke Regeln dafür haben ([Regelwerke](#regelwerke)).

Die Sammler laufen im Supervisor außerhalb von Firejail; das Verzeichnis ist
für den Agenten ausgeblendet. Mit echtem DNS (`--dns`) werden Namen aufgelöst
und erscheinen nicht mehr als verweigerte Anfragen. Logs sind nicht
größenbegrenzt. `activity.jsonl` enthält die Zeitleiste dessen, was die Agenten
taten (siehe [Aktivität der Agenten](#aktivität-der-agenten)).

**Wer abgewiesen wurde.** Jeder Eintrag im Verbindungslog, jede abgewiesene
Namensauflösung und jedes Paket, das die Firewall verworfen hat, nennt den
Prozess dahinter:

- `process`: `pid`, wie der Supervisor der Sandbox sie sieht, und `ns_pid`,
  wie die eigenen Werkzeuge des Agenten sie zeigen, `comm`, `argv` (höchstens
  32 Argumente und 2.000 Zeichen, geschwärzt wie in `processes.jsonl`),
  `exe`, `cwd`, `uid`, `parents` (die Namen von bis zu vier Vorfahren, etwa
  `bash`, `claude`, `tmux: server`) und `start`, die Startzeit, die den
  Prozess von einem späteren mit derselben PID unterscheidet.
- `sandbox`: der Lauf, der Agent, das Backend, die Rolle (`agent`, oder
  `mcp-server` für die Sandbox eines MCP-Servers) und, falls vorhanden, Pod
  oder Session.

Der Supervisor, PID 1 der Sandbox und außerhalb von Firejail, findet den
Prozess über den Socket. Er sucht die Inode des Sockets in `/proc/net/tcp`,
`tcp6`, `udp` und `udp6` und dann den Prozess, der diese Inode hält, in
`/proc/*/fd`. Nichts, was der Agent sendet, etwa ein Header oder eine
Variable, zählt dafür. Ein verworfenes Paket wird über sein Ziel zugeordnet:
Ein TCP-Socket bleibt in `SYN_SENT`, solange er es erneut versucht, und
`state` hält das fest. Namen und Befehlszeilen wählt der Prozess selbst,
deshalb erscheinen beide maskiert und gekürzt. Eine Suche ergibt
`{"unknown": warum}` statt eines Prozesses, wenn sie scheitert, wenn sie zu
lange dauert (eine Abweisung wartet 50 ms auf sie, jeder andere Eintrag
250 ms), wenn die PID inzwischen einem anderen Prozess gehört, oder wenn sie
eine von mehr als 50 Suchen je Sekunde ist. Sie hält nie eine Verbindung auf.

**Was gesendet wurde.** Eine abgewiesene Verbindung hält auch fest, was der
Client geschickt hat: den Servernamen (`sni`) und `alpn` eines TLS-ClientHello,
oder `request_line` und `http_host` bei unverschlüsseltem HTTP. Eine
abgewiesene HTTP-Anfrage hält ihre Header fest, mit wie immer geschwärzten
Zugangsdaten. `--log-debug` (oder `"log_debug": true` in der Projektdatei)
ergänzt mehr: die Bodies abgewiesener Anfragen bis zur Body-Grenze, die
TLS-Versionen, die Zahl der Cipher und die Erweiterungen des ClientHello, und
für den Prozess die ganze Kette seiner Vorfahren mit ihren Befehlszeilen und
die Sockets, die er hält. Zugangsdaten bleiben auch im Debug-Modus
geschwärzt.

Das MCP-Gateway hält den aufrufenden Prozess nur fest, wo sein Relay auf dem
Host läuft, wie bei Image-Servern aus dem Katalog: Der Kernel nennt den Prozess
am anderen Ende des Sockets (`SO_PEERCRED`). Ein Relay in der eigenen Sandbox
eines Servers sieht keinen Prozess einer anderen Sandbox, und welche
Agenten-Sandbox sich verbunden hat, ist dort nicht bekannt.

`sandbox-ai logs` (Standard: das Log-Verzeichnis des Wizards für das aktuelle
Projekt) zählt blockierte Ziele und Dateiereignisse und schlägt für jedes
blockierte Ziel die Option vor, die es freigeben würde: eine Vorlage für
bekannte Hosts, `--profile frontier` für Modell-APIs, `--allow-host` für
andere Namen, `--allow-tcp IP:PORT`, `--allow-host-port` für den Loopback
dieses Hosts oder `--allow IP`. Jeder Vorschlag erweitert die Sandbox, also
vorher prüfen. Namen und Pfade stammen vom Agenten: Sie werden mit allen
Steuerzeichen escaped angezeigt, und ein Name, der kein gültiger Hostname ist,
bekommt keinen Vorschlag.

Außerdem fasst er zusammen, was das Verbindungs-Log und `processes.jsonl`
festgehalten haben: HTTP-Anfragen je Host, geprüfte und abgewiesene Pushes,
Aufrufe von Modell-APIs mit der gemeldeten Token-Nutzung (Antworten im Stil
von Anthropic und OpenAI, gestreamt oder nicht), welche Anmelde-Header an
welchen Host gingen (nur Namen; die Werte sind im Log geschwärzt),
abgewiesene Verbindungen, die gestarteten Programme und die Tool-Aufrufe der
mit `sandbox-ai mcp` gestarteten MCP-Server (aus deren `mcp-calls.jsonl`, mit der
Zahl der nicht ohne Weiteres erlaubten). Am Ende stehen die neuesten
Abweisungen: was, warum, der Prozess und seine Befehlszeile, die Sandbox und
was der Client gesendet hat.

## Aktivität der Agenten

`sandbox-ai activity` zeigt, was die Agenten in einer Sandbox getan haben, als
eine Zeitleiste: Ihre Eingaben, ihre Antworten, jeden Tool-Aufruf mit
Argumenten, Ergebnis und Dauer, die Shell-Befehle, Dateiänderungen, erlaubte
und abgewiesene Verbindungen, Rückfragen, MCP-Tool-Aufrufe, Start, Pause,
Fortsetzen, Anhängen und Ende der Sitzung sowie die Tokens jeder Runde.
Subagenten (Claudes Task/Agent-Tool, die Agenten, die Codex startet, die
Task-Sitzungen von OpenCode) erscheinen unter ihrem eigenen Namen.

Während eine Agenten-Sandbox mit Log-Verzeichnis läuft (`--log-dir`, oder das
Standardverzeichnis, das der Verbindungs-Proxy führt, sofern nicht
`--no-proxy`), sammelt der Starter die Zeitleiste in `activity.jsonl` in diesem
Verzeichnis. Er liest die eigenen Protokolle der Agenten im HOME der Sandbox
und die Logs von sandbox-ai:

| Quelle | Gelesen aus | Format geprüft gegen |
|---|---|---|
| Claude Code | `~/.claude/projects/*/SITZUNG.jsonl`; Subagenten in `SITZUNG/subagents/agent-*.jsonl` | 2.1.317 |
| Codex | `~/.codex/sessions/JJJJ/MM/TT/rollout-*.jsonl`, `~/.codex/archived_sessions/` (Verlauf „legacy“ und „paginated“) | rust-v0.77.0 bis rust-v0.166.0 |
| OpenCode | `~/.local/share/opencode/opencode*.db` (SQLite, ab v1.2.0); `storage/session\|message\|part/` (JSON, davor) | v1.1.48, v1.19.5 |
| sandbox-ai | `files.jsonl`, `egress-denied.jsonl`, das `connections-*.jsonl` des Laufs, die Rückfragen des Laufs | |

Er fragt alle zwei Sekunden ab (eine OpenCode-Datenbank höchstens alle zehn)
und liest alles noch einmal, wenn der Agent beendet ist, bevor ein
Wegwerf-HOME gelöscht wird. Es zählt nur, was nach dem Start geschrieben wurde:
ältere Unterhaltungen in einem behaltenen HOME (aufbewahrte Starts, `--state-dir`, Sessions)
erscheinen nicht noch einmal.

Alles im HOME schreibt der Agent. Dateien werden geöffnet, ohne irgendwo auf
dem Weg einem symbolischen Link zu folgen, höchstens 16 MiB je Lesevorgang;
beschädigte Zeilen werden übersprungen; eine OpenCode-Datenbank wird (bis
512 MiB, nie über einen Link) in ein privates Verzeichnis kopiert und dort mit
den defensiven Einstellungen von SQLite gelesen. Eingaben, Antworten und
Befehle bleiben bis 2000 Zeichen erhalten, andere Werte bis 500;
Dateiinhalte und Ausgaben von Tools werden nicht kopiert (nur ihre Größe; ein
fehlgeschlagener Aufruf behält die ersten 300 Zeichen seines Fehlers). Tokens,
Schlüssel und Passwörter werden geschwärzt: mit den Mustern des
Verbindungs-Logs (Anmelde-Header, Token-Formate, Passwörter in URLs, Optionen
wie `--token`), Zuweisungen wie `API_KEY=…`, privaten Schlüsseln und den
Werten von `--env` und `--secret-env`; Umgebungsvariablen erscheinen nur mit
ihrem Namen. `activity.jsonl` hat den Modus `0600` und endet bei 256 MiB.

```bash
sandbox-ai activity                        # alle Läufe des aktuellen Projekts
sandbox-ai activity claude-3f9a1c2e        # ein Lauf, laufend oder beendet
sandbox-ai activity --session dev -f       # eine Session, live
sandbox-ai activity -k command,file -v     # nur Befehle und Dateiänderungen, mit Details
sandbox-ai activity --since 2h --json      # JSON-Zeilen für Skripte
sandbox-ai activity --runs                 # die Läufe eines Log-Verzeichnisses
sandbox-ai activity --home ~/.local/state/sandbox-ai/sessions/dev/home  # ein HOME direkt lesen
```

```
2026-11-04
  10:00:00  prompt    claude          fix the tests
  10:00:00  network   sandbox         allowed: POST https://api.anthropic.com/v1/messages → 200 (claude)
  10:00:01  reply     claude          Running the tests.
  10:00:01  command   claude          pytest -q tests · exit 1 · 2.5 s
  10:00:05  file      claude          modified /work/a.py (Edit)
  10:00:05  mcp       claude          github: get_issue · denied
  10:00:05  tool      claude          subagent Explore: Find the parser
  10:00:06  tool      claude/Explore  Grep def parse in /work
  10:00:07  approval  claude          denied: mcp__github__get_issue
  10:00:09  network   sandbox         denied: 198.51.100.7:22 (SSH to a public Git host) (rule no-ssh, ssh)
  10:00:10  tokens    claude          2 model call(s) to claude-opus-5-5: input_tokens 20, …
```

Die Agenten-Spalte zeigt `sandbox` für das, was sandbox-ai selbst beobachtet
hat (Dateien, Netzwerk, Rückfragen, die Sitzung). Optionen: `LAUF`,
`-s/--session NAME`, `-p/--project PFAD`, `-l/--log-dir VERZ` oder
`--home VERZ` wählen die Zeitleiste (Standard: alle Läufe des aktuellen
Projekts); `-f/--follow` zeigt laufend neue Ereignisse; `--since ZEIT` (`10m`,
`2h`, `3d`, `14:30`, `2026-11-04`, eine ISO-Zeit), `-k/--kind ART[,ART]` und
`-g/--grep TEXT` grenzen ein; `-n/--tail N` zeigt die letzten N; `-v` die
Details; `-j/--json` die Datensätze; `--runs` listet die Läufe. Die
Desktop-App zeigt dasselbe auf ihrer Seite „Aktivität“.

Jede Zeile von `activity.jsonl` ist ein Datensatz:

| Feld | Bedeutung |
|---|---|
| `v` | Schema-Version (1) |
| `time` | Zeitpunkt in UTC: `2026-11-04T10:00:01.500Z` |
| `run`, `session` | der Lauf (`claude-3f9a1c2e`, `session-NAME`) und der Session-Name oder `null` |
| `agent` | der Agent des Laufs: `claude`, `codex` oder `opencode` |
| `kind` | eine der Arten unten |
| `summary` | eine Zeile |
| `details` | alles Weitere: `tool`, `arguments`, `status` (`ok`, `error`, `denied`, `running`, …), `exit_code`, `duration_ms`, `path`, `verdict`, `rule`, `process`, `usage` … |
| `source` | `claude`, `codex`, `opencode` (das Protokoll des Agenten) oder `files`, `egress`, `proxy`, `approvals`, `sandbox-ai` (von sandbox-ai beobachtet) |
| `id` | die Kennung des Ereignisses; ein späterer Datensatz mit derselben `id` und `"update": true` ersetzt ihn (ein Tool-Aufruf, der nach fünf Sekunden noch offen ist, erscheint als `running` und noch einmal, wenn er endet) |
| `conversation`, `subagent` | die eigene Sitzungskennung des Agenten und der Subagent, wenn bekannt |

| Art | Was |
|---|---|
| `prompt` | was Sie gefragt haben (bei einem Subagenten: was sein Agent fragte), vorgemerkte Eingaben, Slash-Befehle |
| `reply` | der Antworttext des Agenten |
| `tool` | andere Tool-Aufrufe: Lesen, Suchen, Web, Subagenten, Pläne |
| `command` | Shell-Befehle, die der Agent mit seinem Tool ausführte (und `!`-Befehle, die Sie eingaben), mit Exit-Code und Dauer |
| `file` | angelegte, geänderte, gelöschte oder verschobene Dateien: durch die Bearbeitungs- und Patch-Tools der Agenten, durch Befehle (Claude meldet sie) und wie inotify sie sah (`files.jsonl`, mit `--log-dir`; keine Lesezugriffe; mehr als fünf in einem Ordner auf einmal sind ein Ereignis; der eigene Zustand der Agenten wie `~/.claude` bleibt außen vor) |
| `network` | abgewiesene Namensauflösungen und Verbindungen, geprüfte und abgewiesene Pushes, die erste Anfrage an jeden Host (mit Regel und Prozess, wenn der Proxy sie nennt); Wiederholungen werden gezählt und einmal pro Minute gezeigt |
| `approval` | die Rückfragen der Sandbox mit ihren Antworten; Tool-Aufrufe, die die eigene Berechtigungsprüfung des Agenten ablehnte |
| `mcp` | MCP-Tool-Aufrufe (`mcp__server__tool`, Codex' Namensraum `mcp__server`, OpenCodes `server_tool`) |
| `session` | Start (Workspaces, Backend), Pause, Fortsetzen, Anhängen, Lösen, Stopp-Anforderung, Ende mit Exit-Status; Unterbrechungen und Modellfehler |
| `tokens` | die Modellnutzung jeder Runde (Claude: einmal je Nachricht; Codex: `token_count`; OpenCode: je Antwort, mit Kosten) |

Codex speichert Rückfragen nicht in seinen Rollouts, OpenCode nur
Ablehnungen: ihre eigenen Berechtigungsabfragen erscheinen als Tool-Aufrufe
mit Status `denied`. inotify nennt keinen Prozess. Eine Agentenversion, die ihr
Format ändert, kann einige Ereignisse verbergen, bis sandbox-ai sie
unterstützt: unbekannte Zeilen werden übersprungen, nie ein Abbruch.

## Telemetrie der Agenten

Claude Code, Codex und OpenCode können an ihre Hersteller melden:
Nutzungsstatistiken, Fehlerberichte, Feedback und Fehlermeldungen mit Ihrem
Gespräch, geteilte Sitzungen, Update-Prüfungen. sandbox-ai schaltet das in
jeder Sandbox **standardmäßig aus**, für den Hauptagenten, die Agenten
`--with` und Helfer, mit den eigenen Schaltern jedes Agenten (geprüft in Claude
Code 2.1.317, Codex 0.166 und OpenCode 1.19.5):

| Agent | `--telemetry off` (Standard) | `strict` zusätzlich |
|---|---|---|
| Claude Code | `DISABLE_TELEMETRY`, `DISABLE_ERROR_REPORTING`, `DISABLE_BUG_COMMAND`, `CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY`, `DISABLE_AUTOUPDATER` | `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` (auch Plugin- und Marketplace-Downloads, Release Notes) |
| Codex | in `~/.codex/config.toml` der Sandbox: `analytics.enabled = false`, `feedback.enabled = false`, `otel.metrics_exporter = "none"`, `check_for_update_on_startup = false` | `features.plugins = false` (der Plugin-Abgleich) |
| OpenCode | `OPENCODE_DISABLE_AUTOUPDATE`; `"share": "disabled"`, `"autoupdate": false` in `OPENCODE_CONFIG_CONTENT` | `OPENCODE_DISABLE_MODELS_FETCH`, `OPENCODE_DISABLE_LSP_DOWNLOAD` |

OpenCode sendet selbst keine Nutzungsstatistiken; OpenTelemetry exportiert es
nur an einen Endpunkt, den Sie einstellen.

Jeder Agent bekommt außerdem die Schalter von Werkzeugen, die er starten kann:
`DO_NOT_TRACK=1`, `OPENSPEC_TELEMETRY=0`, `HF_HUB_DISABLE_TELEMETRY`,
`NEXT_TELEMETRY_DISABLED`, `DOTNET_CLI_TELEMETRY_OPTOUT`,
`GATSBY_TELEMETRY_DISABLED`, `ASTRO_TELEMETRY_DISABLED`.

```bash
sandbox-ai claude                       # aus
sandbox-ai claude --telemetry strict    # nur noch die Aufrufe des Modells
sandbox-ai claude --telemetry on        # die Einstellungen der Agenten
```

- **Dein Standard** für jeden Start: `"telemetry": "off" | "strict" | "on"` in
  `~/.config/sandbox-ai/defaults.json`; `--telemetry` auf der Befehlszeile
  gewinnt.
- **Sicherheitsmodi:** `high` lehnt `--telemetry on` ab; bei `paranoid` ist
  `strict` das Mindeste (ein Standard von dir darunter wird angehoben, eine
  Option darunter abgelehnt).
- **Eine Projektdatei** (`.sandbox-ai.json`) darf es strenger machen, nie
  einschalten: `--telemetry on` wird dort abgelehnt.
- **Was Sie selbst setzen, gewinnt:** eine Variable, die Sie mit `--env` oder
  `--secret-env` übergeben, und was die `~/.codex/config.toml` eines
  behaltenen HOME schon sagt (der Start nennt jeden solchen Schlüssel). Eine
  `config.toml`, die sich nicht sicher ergänzen lässt (ein inline
  `analytics = {...}`, Schlüssel einer Tabelle nur als gepunktete Schlüssel),
  bleibt wie sie ist, mit einer Warnung.
- **Der Proxy lehnt sie ebenfalls ab**, wo sie an einen Host geht, den der
  Agent ohnehin braucht (der Regelsatz `telemetry`): das Ereignisprotokoll von
  Claude Code (`api.anthropic.com/api/event_logging/**`) und die Analytics von
  Codex (`chatgpt.com/backend-api/codex/analytics-events/**`), auch wenn ein
  Agent seine Schalter missachtet; die Anfragen an das Modell gehen durch.
  Andere Telemetrie-Hosts (Datadog, Sentry, Statsig) gehören nicht zu dem, was
  eine Sandbox standardmäßig erreicht. Ein Host mit `--no-inspect` bekommt keine
  solche Regel (der Proxy sieht seine Pfade nicht). Der Probelauf (`-n`) zeigt
  unter `"telemetry"` die Stufe, woher sie kommt und jede Variable und jeden
  Schlüssel, den sie setzt.

## Datenschutzprüfung vor dem Veröffentlichen

Ein Agent, der pushen darf, kann veröffentlichen, was Ihren Rechner nie
verlassen sollte: Ihren Home-Pfad in einer Konfigurationsdatei, die Adresse
Ihres LLM-Servers im LAN, Ihren Kontonamen auf dem eigenen Git-Server, ein
Token. sandbox-ai sucht danach, **bevor** es veröffentlicht ist, an drei
Stellen:

- **Nach jedem Lauf** auf dem Host: Für jeden beschreibbaren Workspace, der
  ein Git-Repository mit öffentlichem Remote ist, zeigt es, was der nächste
  Push veröffentlichen würde und privat bleiben sollte. Ein Remote ist
  öffentlich, sofern er kein lokaler Pfad ist, nicht auf dem Loopback dieses
  Rechners oder in einem privaten Netz liegt (RFC 1918, `.lan`, `.local`, …)
  und keiner Ihrer eigenen Git-Server aus `--git-token-*` oder
  `--git-credentials` ist.
- **In der Sandbox** als Git-Hooks für den Agenten: `pre-commit` warnt vor
  gestagten Änderungen, `pre-push` zu einem öffentlichen Remote **verweigert**
  blockierende Funde und sagt dem Agenten, sie zu entfernen oder Sie zu
  fragen. Danach läuft der eigene Hook des Repositorys (`core.hooksPath` des
  Repositorys oder `.git/hooks`).
- **Von Hand**: `sandbox-ai privacy-check [PFAD ...]`, auch für Repositorys,
  die Sie vom Host aus pushen.

„Was der nächste Push veröffentlichen würde“ heißt: Commits auf lokalen
Branches, die kein Remote-Tracking-Branch hat (die Zeilen, die sie
hinzufügen, Autor, Committer und Nachricht), gestagte Änderungen, im
Arbeitsbaum geänderte Dateien und nicht ignorierte, ungetrackte Dateien.
Zeilen, die vorher schon da waren, werden nicht erneut gemeldet;
`privacy-check --all` prüft jede Datei.

Die Regeln kommen aus drei Quellen:

| Quelle | Was sie findet | Schwere |
|---|---|---|
| eingebaut | private Schlüssel; Tokens von AWS, GitHub, GitLab, Anthropic, OpenAI, Slack, Google, Stripe, npm, PyPI, Hugging Face | block |
| aus diesem Lauf abgeleitet | Ihr Home-Pfad auf dem Host, private Adressen aus `--allow-tcp`, Ihre Kontonamen auf eigenen Git-Servern, die übergebenen Tokens (nie angezeigt, nur maskiert) | block |
| | Ihre Git-E-Mail-Adresse (keine noreply-Adresse), der Name Ihres Rechners, Ihr eigener Git-Server auf diesem Rechner | warn |
| eigene Dateien | `global.rules.toml` und `PROJEKT.rules.toml` (oder `.json`; `PROJEKT` ist der Verzeichnisname des Workspaces) in `--privacy-rules VERZ`, Standard `~/.config/sandbox-ai/privacy` | wie angegeben |

Eigene Regeldateien haben das Format des Datenschutz-Tors `scrub_guard`:

```toml
[[pattern]]
id = "internal-codename"
regex = '(?i)\bbluefin\b'
severity = "block"            # oder "warn"
reason = "Codename eines unveröffentlichten Produkts"

[[allow]]                     # eine Zeile, auf die das passt, wird nicht gemeldet
id = "public-docs"
regex = 'bluefin-sdk'
path_glob = "docs/*"          # optional
reason = "öffentlicher Name des SDK"
```

Das Verzeichnis muss außerhalb aller Workspaces, rw-Mounts und des
`--state-dir` liegen: Es enthält genau die Zeichenketten, die es verbietet,
und gelangt deshalb nie in die Sandbox. Die Hooks des Agenten bekommen nur
die eingebauten und abgeleiteten Regeln, ohne die Tokens. Eine Regeldatei,
die nicht lesbar ist, stoppt den Start; TOML braucht Python 3.11 oder das
Paket `tomli`, JSON geht überall.

`--privacy-check always` prüft nach dem Lauf jeden beschreibbaren
Git-Workspace, `off` schaltet Prüfung und Hooks ab. `privacy-check` endet mit
1 bei blockierenden Funden und mit 2, wenn es nicht prüfen kann (Regeln nicht
lesbar, kein Repository).

**Grenzen.** Die Hooks schützen vor Versehen, nicht vor einem Agenten, der
veröffentlichen will: `git push --no-verify`, `git -c core.hooksPath=…`, die
GitHub-API oder `gh` umgehen sie. Die Prüfung nach dem Lauf kommt nach einem
solchen Push. Muster finden, was sie beschreiben; ein Geheimnis in
unbekanntem Format, in Binärdateien oder in Dateien über 1 MiB wird nicht
gefunden. Auf dem Host führt die Prüfung nur Git-Plumbing-Befehle aus, die
kein Programm aus der Konfiguration des Repositorys starten (keine Hooks,
kein fsmonitor, keine Filter, kein textconv), und liest Dateien des
Arbeitsbaums, ohne Symlinks zu folgen.

## Verbindungs-Proxy

Jede TCP-Verbindung des Agenten läuft durch einen Proxy, den sandbox-ai mit
der Sandbox startet. Er ist standardmäßig an; `--no-proxy` schaltet ihn ab
(und mit `--offline` gibt es nichts weiterzuleiten).

- **Transparent:** Die Firewall leitet jede Verbindung um (nftables-NAT im
  Netzwerk-Namespace der Sandbox). Programme brauchen keine
  Proxy-Einstellungen. Der Proxy verbindet sich selbst mit dem ursprünglichen
  Ziel, weiterhin hinter der Freigabeliste der Firewall, und lehnt sofort ab,
  was die Netzwerkoptionen nicht erlauben.
- **TLS wird aufgebrochen** mit einer CA nur für diesen Lauf (Schlüssel und
  Zertifikate bleiben im privaten Verzeichnis des Proxys und verschwinden mit
  dem Lauf). Der Agent vertraut dieser CA über das System-Bundle und über
  `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, `GIT_SSL_CAINFO`,
  `PIP_CERT`, `CARGO_HTTP_CAINFO`, `NODE_EXTRA_CA_CERTS` und ähnliche
  Variablen. Der Proxy prüft den echten Server gegen die CAs des Systems. Nur
  HTTP/1.1 wird angeboten; WebSockets funktionieren, QUIC/HTTP3 (UDP) läuft
  nicht über den Proxy.
- **Protokolliert:** jede HTTP-Anfrage mit Methode, voller URL, Status,
  Headern und Bodies (jeweils die ersten 64 KiB, gzip/deflate entpackt), jede
  andere Verbindung mit Ziel, Bytes und Dauer, und alles Abgelehnte.
  Zugangsdaten werden geschwärzt: `Authorization`, Cookies, Header mit Namen
  wie Key, Token oder Secret, tokenartige Query-Parameter und JSON-Felder sowie
  bekannte Token-Formate. Das Log ist `connections-ZEIT-LAUF.jsonl` im
  Logverzeichnis: `--log-dir`, sonst
  `~/.local/state/sandbox-ai/logs/PROJEKT-HASH`. Jeder Lauf schreibt eine
  eigene Datei; die ältesten werden gelöscht, sobald sie zusammen 1 GiB
  übersteigen. Nach dem Lauf zeigt sandbox-ai eine Zusammenfassung.
- **Push-Prüfung im Netz:** Ein Git-Push über HTTP(S) wird angehalten, bis die
  Datenschutzprüfung ihn gesehen hat. Der Proxy liest die Ref-Updates, ein
  Prüfprozess (ohne jede Capability, außerhalb von Firejail) sucht die Commits
  in den Workspaces und prüft, was die Gegenseite noch nicht hat, mit allen
  Regeln einschließlich Ihrer eigenen (die der Agent nie sieht). Ein
  blockierender Fund lehnt den Push so ab, wie es ein Git-Server tut; die Funde
  erscheinen als `remote:`-Zeilen. Hooks, `--no-verify` oder ein anderer
  `core.hooksPath` ändern daran nichts. Ein Push, den der Prüfprozess nicht
  prüfen kann (Commits aus einem Klon außerhalb der Workspaces, unbekanntes
  Format), wird ebenfalls abgelehnt.
- **SSH** wird abgelehnt, außer zu Bereichen, die mit `--allow` erlaubt sind,
  und zu dem Port, der mit `--allow-tcp IP:PORT` angegeben ist (beim Start
  oder mit `sandbox-ai net allow`); nie zu einem öffentlichen Git-Host oder in
  einen Bereich, der einen enthält, und nie zu dem, was nur per Rückfrage
  erlaubt wurde, weil ein Push über SSH die Prüfung umginge. Mit
  `--privacy-check off` werden weder Pushes noch SSH geprüft.

Die Push-Prüfung findet, was ihre Regeln beschreiben; sie verhindert
versehentliches Veröffentlichen, nicht einen entschlossenen Agenten: absichtlich
kodierte Daten (base64, ein Archiv, Verschlüsselung) kommen an jeder
Musterprüfung vorbei. Das Verbindungslog hält in jedem Fall fest, was gesendet
wurde.

Programme mit Certificate Pinning oder eigener CA-Liste lehnen die
Sitzungs-CA ab; der Proxy loggt so eine Verbindung als `tls-refused` mit
Hinweis. Mit `--no-inspect HOST` (oder `--no-inspect '*.example.org'`)
läuft TLS zu diesem Host ungeöffnet durch: Geloggt werden nur Name und Bytes,
Pushes dorthin werden nicht geprüft; öffentliche Git-Hosts lassen sich daher
nicht ausnehmen, solange die Datenschutzprüfung an ist. Dasselbe gilt für
Server, deren Zertifikat das System nicht kennt (ein interner Server mit
eigener CA): `tls-upstream-failed` im Log.

**Anfragen je Host.** Ein erlaubter Host erlaubt jede Anfrage an ihn: Mit
`--allow-github` könnte ein Agent über `api.github.com` auch Gists oder Issues
anlegen. `--allow-http 'HOST METHODEN PFAD'` grenzt das ein: Sobald ein Host
(oder `*.DOMAIN`) Regeln hat, erreichen ihn nur passende Anfragen, alles andere
bekommt ein `403`, das der Agent lesen kann, und eine `denied`-Zeile im Log.
Im Pfad passt `*` innerhalb eines Segments und `**` über Segmente hinweg; Pfade
werden so verglichen, wie der Server sie auflöst: dekodiert, ohne Query, mit
aufgelösten `..` und doppelten Schrägstrichen, ohne `;Parameter`. Eine Anfrage
mit zwei `Host`-Headern wird abgewiesen.

```bash
sandbox-ai claude --allow-github \
  --allow-http 'api.github.com GET,HEAD /**' \
  --allow-http 'api.github.com POST /repos/me/app/pulls'
```

Die Regeln gelten für jeden Namen, den eine Anfrage erreichen kann: den
geprüften Servernamen, den `Host`-Header (ein Zertifikat deckt oft viele Namen
ab, und Server wie GitHub leiten nach `Host` weiter) und die gepinnten Adressen
des Hosts; ein anderer Name oder die nackte IP führt also nicht an ihnen vorbei,
und ein `Host`, der vom geprüften Namen abweicht, wird abgewiesen, wo Regeln
gelten. Sie brauchen den Proxy, und ein
Host unter `--no-inspect` kann keine haben. `--allow-http` ist ein
[Regelwerk](#regelwerke) unter anderen: Regeldateien ergänzen `deny` und `ask`,
Bedingungen an Query, Header und JSON-Body sowie Regel-IDs im Log.

| Im Log als | Bedeutung |
|---|---|
| `http` | Eine Anfrage mit Antwort (`push` enthält das Urteil der Prüfung) |
| `tls-passthrough` | TLS ungeöffnet durchgereicht (`--no-inspect`) |
| `tls`, `tcp` | Andere Protokolle, in TLS oder unverschlüsselt |
| `upgraded` | Ein WebSocket nach dem Upgrade |
| `denied` | Von den Netzwerkoptionen oder einer Regel nicht erlaubt, oder SSH abgelehnt; mit dem Prozess und dem, was der Client gesendet hat ([Logs](#egress--und-dateizugriffs-logs)) |
| `tls-refused`, `tls-upstream-failed` | Das Programm lehnte die Sitzungs-CA ab, oder dem Zertifikat des Servers wird nicht vertraut |

Der Proxy läuft als eigener Benutzer in der Sandbox, ohne Capabilities und mit
`no_new_privs`; nur diesen Benutzer nimmt die Umleitung der Firewall aus. Er
verarbeitet, was der Agent sendet, bleibt dabei aber in den Namespaces der
Sandbox. Er braucht die nftables-NAT-Module auf dem Host (`nft_chain_nat`,
`nft_redir`; auf den meisten Systemen geladen, sonst
`sudo modprobe -a nft_chain_nat nft_redir`) und `openssl` im Image (enthalten)
bzw. mit Bubblewrap auf dem Host.

## Rückfragen und Browser-Anfragen

Mit Proxy fragt eine Sandbox nach, statt still zu scheitern (`--ask on`, der
Standard):

- eine **Verbindung** zu einem Ziel, das die Netzwerkoptionen nicht erlauben
  (sie wartet auf die Antwort). Die Frage zeigt Adresse, Protokoll und den
  Servernamen, den der Agent in TLS oder im `Host`-Header geschickt hat.
  Dieser Name ist eine Angabe des Agenten und nicht geprüft: Erlaubt wird die
  Adresse, für jedes Protokoll außer SSH;
- eine **Namensauflösung** eines nicht gepinnten Namens (sofort abgewiesen;
  erlauben Sie ihn, löst sandbox-ai den Namen auf dem Host auf, pinnt seine
  öffentlichen Adressen und öffnet HTTPS zu ihnen, sodass der nächste Versuch
  des Agenten klappt; ein Name, der nur auf private, Loopback- oder
  Link-Local-Adressen zeigt, wird abgewiesen, solche erlauben Sie ausdrücklich
  mit `sandbox-ai net allow`);
- ein **Push**, den die Datenschutzprüfung durchlässt, zu dem sie aber
  Warnungen hat (Pushes mit blockierenden Funden werden ohne Frage
  abgewiesen);
- eine **Anfrage** oder ein **Aufruf eines MCP-Werkzeugs**, den eine
  `ask`-Regel anhält (siehe [Regelwerke](#regelwerke)); höchstens für den Lauf
  erlaubt;
- ein **Gerät**, nach dem eine Geräteregel fragt, bevor es eingehängt wird
  (beim Start, bei `sandbox-ai devices add` oder beim Neuanstecken; siehe
  [Hardware](#hardware-geräte-und-gpus)); höchstens für den Lauf erlaubt;
- das **Öffnen einer URL** in Ihrem Browser: `xdg-open`, `sensible-browser`,
  `x-www-browser`, `www-browser`, `open` und `$BROWSER` in der Sandbox bitten
  den Host, http(s)-URLs zu öffnen, eine Frage je URL (etwa die Login-Seite
  eines Werkzeugs). Ohne Ihre Antwort öffnet sich nichts; das Programm in der
  Sandbox erfährt nur, dass gefragt wurde.

Jede Frage nennt den Prozess dahinter, etwa `connect to 203.0.113.9:443 (tls),
named example.org by the agent (not verified); asked by curl (pid 7,
/usr/bin/curl) in claude-1a2b3c4d` (siehe [Logs](#egress--und-dateizugriffs-logs)).

Jede Frage erscheint als Desktop-Benachrichtigung (`notify-send`, falls
vorhanden; ihr Titel nennt die fragende Sandbox: Lauf, Workspace und
Session), in der App und mit den Befehlen unten. Ab libnotify 0.7.9 hat die
Benachrichtigung Knöpfe, die sie beantworten: **Allow**, **Allow always**,
**Block** und **Block always** (eine zu öffnende URL: **Open**, **Ignore**).
*Always* wird für das Projekt gemerkt, in beide Richtungen: Was immer blockiert
ist, wird bei jedem späteren Start ohne Frage abgewiesen, und wer es später
immer erlaubt, hebt die Sperre auf.

```sh
sandbox-ai approvals list                     # ID  LAUF  was gefragt wird
sandbox-ai approvals allow ID --scope run     # once (Standard), run, always
sandbox-ai approvals deny ID [--scope once|always] # Standard: in diesem Lauf nicht erneut gefragt; always: fürs Projekt blockiert
sandbox-ai approvals history [LAUF]           # Fragen und ihre Antworten
sandbox-ai approvals remembered [--json]      # was je Projekt immer erlaubt/blockiert ist: Name oder Adresse, wie, wann, aus welcher Frage
sandbox-ai approvals forget PROJEKT EINTRAG   # einen davon vergessen (Hostname oder IP:PORT)
```

Eine Frage ohne Antwort wird nach 60 Sekunden abgewiesen und, wie eine
Ablehnung, in diesem Lauf nicht erneut gestellt. `once` erlaubt diese eine
Verbindung (die Adresse ist 30 Sekunden offen), `run` bis zum Ende der
Sandbox, `always` zusätzlich jeden späteren Start desselben Projekts (bei
einer Namensauflösung der Name, bei einer Verbindung der Name, wenn er sich
belegen lässt, sonst die Adresse, siehe unten; steht dann in
`~/.config/sandbox-ai/approved.json`, sobald die Sandbox die Antwort angewandt
hat; URLs mit Benutzerteil (`https://github.com@evil.example/`) werden
abgewiesen, und jede URL-Frage nennt den Host, den sie öffnet; Einträge dort löschen, um sie zu
vergessen; der Launcher nennt die übernommenen). Eine mit `once` erlaubte
Namensauflösung bleibt für den Lauf gepinnt; ein Push wird höchstens für den
Lauf erlaubt. Höchstens 20 Fragen sind gleichzeitig offen und 500 je Lauf;
darüber hinaus weist die Sandbox ohne Frage ab. Alles, was der Agent wählt
(Namen, URLs, Refs), erscheint mit maskierten Steuerzeichen und Nicht-ASCII,
sodass er keine Zeilen in der Liste vortäuschen kann. Telemetrie-Hosts,
die die Agenten im Hintergrund ansprechen (`statsig.com`, `sentry.io` und
ähnliche), werden ohne Frage abgewiesen. `--ask off` weist alles ab, was die
Optionen nicht erlauben, wie bisher; `--no-proxy` und `--offline` fragen nichts.

Die Fragen liegen im Steuerverzeichnis des Laufs (unter `$XDG_RUNTIME_DIR`),
das der Agent nicht sieht; nur Prozesse Ihres eigenen Benutzers können
antworten.

**Was "Allow always" für eine Verbindung behält.** Der Name in einer
Verbindungsfrage (TLS-Servername oder `Host`-Header) ist, was der Agent
geschickt hat. sandbox-ai merkt sich deshalb den Namen nur, wenn es ihn
belegen kann, sonst die Adresse. Jede Frage sagt, was es sein wird:

- `Allow always: api.example.com (verified by its certificate)` oder
  `(verified: sandbox-ai pinned it to this address)`: sandbox-ai hat den Namen
  selbst auf diese Adresse gepinnt, oder ein Zertifikat, das der Proxy früher
  im Lauf geprüft hat, gilt für ihn.
- `Allow always: api.example.com if its certificate proves it on this
  connection, else 203.0.113.7:443 (address only)`: Die erlaubte Verbindung
  entscheidet. Der Proxy prüft das Zertifikat des echten Servers gegen die
  CAs des Systems; gilt es für den Namen, wird der Name gemerkt, sonst (ein
  Zertifikat für einen anderen Namen, ein Wildcard, das den Namen nicht
  abdeckt, keine Verbindung binnen 15 Sekunden) die Adresse.
- `Allow always: 203.0.113.7:443 (address only: …)`: Nichts kann einen Namen
  belegen. Beispiele: Es kam kein Name, oder der Name ist eine Adresse; die
  Verbindung ist kein TLS oder geht nicht an Port 443; die Adresse ist nicht
  öffentlich; der Host steht unter `--no-inspect`.

Das Zertifikat wird nie vor Ihrer Antwort geprüft, denn das schickte den
Servernamen, den der Agent gewählt hat, ohne Ihre Erlaubnis an die Adresse,
die er gewählt hat. Ein gemerkter Name wird bei späteren Starts wie
`--allow-host` geöffnet (HTTPS zu den Adressen, auf die er dann zeigt), so
funktioniert er weiter, wenn der Dienst umzieht. Geöffnet werden nur
öffentliche Adressen oder solche innerhalb von `allow_within` des
Administrators. Ein Name, der auf etwas anderes zeigt (ein Dienst, der ins LAN
umgezogen ist, DNS-Rebinding) oder sich nicht mehr auflösen lässt, bleibt mit
einem Hinweis außen vor; der Start geht weiter. Namen, die die Richtlinie des
Administrators nicht in `allow_hosts` führt, werden als Adresse gemerkt.
`sandbox-ai approvals remembered` zeigt für jeden Eintrag, ob er ein Name oder
eine Adresse ist, wie ein Name belegt wurde, wann er hinzukam und aus welcher
Frage.

## Regelwerke

Die Netzwerkoptionen legen fest, welche Hosts der Agent erreicht. Regelwerke
legen fest, was er dort tun darf und welche MCP-Werkzeuge er aufrufen darf.
Sie **erlauben, erfragen oder verbieten** jede HTTP-Anfrage, nach Host,
Methode, Pfad, Query, Headern und JSON- oder Formular-Body, jeden Aufruf
eines MCP-Werkzeugs, nach Werkzeug und Argumenten, und jedes Gerät, das der
Agent bekommt, nach USB-IDs, Seriennummer, Klasse, Subsystem und Pfad. Vorbild
sind die Rechteregeln für Werkzeugaufrufe von Progent. Durchgesetzt werden die
Regeln außerhalb des Agenten: vom Verbindungs-Proxy und vom Relay vor jedem
MCP-Server, zwei Prozessen, die der Agent weder erreicht noch ändern kann, und
für Geräte vom Starter auf dem Host. Regeln für die eigenen Werkzeuge des
Agenten (Bash, Dateiänderungen, WebFetch) laufen über seine Hooks: eine
Leitplanke, keine Grenze (siehe *Die eigenen Werkzeuge des Agenten* unten).

Regelwerke kommen von fünf Stellen. Jedes ist eine JSON-Datei im selben
Format:

| Regelwerk | Gehört |
|---|---|
| `/etc/sandbox-ai/policy.json`, Schlüssel `"rules"` | dem Administrator |
| `~/.config/sandbox-ai/rules.json` (falls vorhanden) | Ihnen, bei jedem Start |
| `--rules DATEI` (wiederholbar; außerhalb aller Workspaces) | Ihnen, für diesen Start |
| `--allow-http 'HOST METHODEN PFAD'` | Ihnen, wie bisher |
| die vertraute Projektdatei, Schlüssel `"rules"` | dem Projekt |

**Jedes Regelwerk entscheidet für sich, und die strengste Entscheidung gilt**
(deny vor ask vor allow). Kein Regelwerk kann erweitern, was ein anderes
einschränkt: Ein `allow` des Projekts hebt nie Ihr `deny` auf, und Ihr
`allow` beantwortet nie ein `ask` des Administrators. Auch öffnet keine Regel
die Firewall: Eine Anfrage, die eine Regel erlaubt, muss trotzdem eine sein,
die die Netzwerkoptionen durchlassen.

`--allow-github` öffnet `api.github.com` für jede Anfrage, der Agent könnte
also auch Gists oder Issues anlegen. Diese Datei hält das fern und lässt den
Rest der API arbeiten:

```json
{
  "version": 1,
  "http": [
    {"id": "no-gists", "host": "api.github.com", "path": "/gists/**", "decision": "deny",
     "reason": "gists can publish workspace data"},
    {"id": "no-issue-writes", "host": "api.github.com", "methods": ["POST", "PATCH", "PUT", "DELETE"],
     "path": "/repos/*/*/issues/**", "decision": "deny"},
    {"id": "releases", "host": "api.github.com", "methods": "POST", "path": "/repos/*/*/releases",
     "decision": "ask"}
  ],
  "mcp": [
    {"id": "reads", "server": "files", "tool": ["read_file", "list_directory"], "decision": "allow"},
    {"id": "writes", "server": "files", "tool": "write_file",
     "args": {"path": {"path_glob": "/work/src/**"}}, "decision": "allow"},
    {"id": "deletes", "server": "files", "tool": "delete_file", "decision": "ask"},
    {"id": "rest", "server": "files", "tool": "*", "decision": "deny", "priority": 1000}
  ]
}
```

```bash
sandbox-ai claude --allow-github --rules ~/.config/sandbox-ai/github.json --mcp files
sandbox-ai claude --rules ~/.config/sandbox-ai/github.json --dry-run   # Datei prüfen: nennt die Regelwerke, stoppt bei Fehlern
```

**Das Format.** Ein Regelwerk hat `"version": 1`, wahlweise `"description"`,
`"defaults"`, die Listen `"http"`, `"mcp"`, `"devices"` und `"tool"`, `"untrusted"` (Quellen
nicht vertrauenswürdiger Inhalte, siehe *der Taint des Laufs* unten) und `"canary"` (was auf das Lesen einer
[Köderdatei](#köderdateien-canaries) und einen abfließenden Canary-Token folgt). Jede Regel hat eine `id`
(bis zu 64 Buchstaben, Ziffern, `.`, `_`, `:` oder `-`; sie nennt die Regel in
Logs und Rückfragen) und eine `decision`: `allow`, `ask` oder `deny`. Eine
Regel kann außerdem eine `priority` haben (Standard 100, kleiner kommt zuerst),
einen `reason` (eine Zeile, die der Agent und die Rückfrage zeigen) und eine
`description`.

| HTTP-Regel | Bedeutung |
|---|---|
| `host` (Pflicht) | Ein Name, `*.DOMAIN` (die Namen unterhalb von DOMAIN), eine Adresse oder `*` für jeden Host; ein String oder eine Liste |
| `methods` | `GET`, `["POST", "PUT"]` oder `*` (Standard) |
| `path` | Ein Muster oder eine Liste (Standard `/**`): `*` passt innerhalb eines Segments, `**` über Segmente hinweg; `/gists/**` deckt auch `/gists` ab |
| `query`, `headers` | `{"NAME": Bedingung}` für Query-Parameter und Header (Header-Namen in beliebiger Schreibweise) |
| `body` | Eine Bedingung an den Body: JSON (egal, welcher Typ angegeben ist) oder ein Formular mit `application/x-www-form-urlencoded` |

| MCP-Regel | Bedeutung |
|---|---|
| `server` | Der Name des MCP-Servers (wie in `--mcp NAME`), eine Liste oder `*` (Standard) |
| `tool` (Pflicht) | Ein Werkzeugname oder ein Muster mit `*`, oder eine Liste |
| `args` | `{"NAME": Bedingung}` für die Argumente des Aufrufs |

| Geräteregel | Bedeutung |
|---|---|
| `usb` | `HERSTELLER:PRODUKT` hexadezimal (`2e8a:000a`) oder `HERSTELLER:*`; ein String oder eine Liste |
| `serial` | Die USB-Seriennummer, `*` als Platzhalter; ein String oder eine Liste |
| `class` | Eine USB-Klasse des Geräts oder einer seiner Schnittstellen: `hid`, `cdc`, `cdc-data`, `video`, `audio`, `mass-storage`, `smart-card`, `wireless`, `printer`, `hub`, `misc`, `vendor` und die übrigen der USB-Spezifikation, oder zwei Hex-Ziffern |
| `subsystem` | Das Kernel-Subsystem des Knotens: `tty`, `usb` (rohes USB), `video4linux`, `sound`, `drm`, `misc` und so weiter |
| `node` | Ein Pfadmuster unter `/dev` (`*` innerhalb eines Segments, `**` darüber hinweg), verglichen mit dem Knoten, dem angegebenen Pfad und den Links, die udev darauf gesetzt hat (`/dev/serial/by-id/...`); `cdi:nvidia.com/gpu=all` für `--gpu nvidia` mit Podman |

Eine Geräteregel braucht mindestens eines davon, und jedes angegebene muss
passen. Geräteregeln beurteilen `--device`, `--gpu` (`/dev/dri` als ein
Knoten), `sandbox-ai devices add` und jedes Neuanstecken. Dieses Regelwerk
gibt dem Agenten ein Board und `/dev/kvm`, fragt vor rohem USB zum Board und
weist alles andere ab:

```json
{
  "version": 1,
  "defaults": {"devices": "deny"},
  "devices": [
    {"id": "pico", "usb": "2e8a:000a", "decision": "allow"},
    {"id": "pico-raw", "usb": "2e8a:000a", "subsystem": "usb", "decision": "ask", "priority": 50,
     "reason": "raw USB to the Pico (picotool, flashing)"},
    {"id": "kvm", "node": "/dev/kvm", "decision": "allow"},
    {"id": "no-cameras", "class": "video", "decision": "deny", "priority": 10}
  ]
}
```

Innerhalb eines Regelwerks werden die Regeln mit der kleinsten `priority` zuerst
versucht, bei gleicher Priorität `deny` vor `ask` vor `allow`, dann in der
Reihenfolge der Datei. Die erste Regel, die passt, entscheidet. Passt keine,
gilt der Reihe nach:

- **Erlaubnislisten.** Sobald ein Host eine `allow`-Regel in einem Regelwerk hat
  (`*` zählt nicht), weist dieses Regelwerk jede andere Anfrage an den Host ab,
  wie `--allow-http` es immer tat. Ebenso weist ein Regelwerk, sobald ein
  Werkzeug eine `allow`-Regel hat, jeden anderen Aufruf dieses Werkzeugs ab,
  und sobald ein Hersteller:Produkt eine `allow`-Regel hat, jedes andere Gerät
  davon (andere Seriennummer, andere Klasse).
  Um einen ganzen Server auf seine erlaubten Werkzeuge zu beschränken, ergänzen
  Sie ein `deny` für sein Werkzeug `*` mit hoher Prioritätszahl, wie oben.
- **Standards.** Sonst gelten die `"defaults"` des Regelwerks: `{"http": "ask"}`,
  `{"mcp": "deny"}`, `{"devices": "deny"}` oder `{"tool": "ask"}`. Ist keiner gesetzt, hat das Regelwerk nichts zu sagen
  und andere Regelwerke entscheiden; gibt es nirgends eine Regel, ist die Anfrage
  oder der Aufruf erlaubt.

**Bedingungen** sind eine Teilmenge von JSON Schema: `type`, `const`, `enum`,
`pattern`, `minLength`, `maxLength`, `minimum`, `maximum`, `items`,
`properties`, `required`, `additionalProperties: false`, `not`, `anyOf` und
`allOf`. sandbox-ai ergänzt `glob` (`*` steht für beliebige Zeichen),
`path_glob` (absolute Pfade, verglichen nach Auflösen von `.`, `..` und
doppelten Schrägstrichen; `*` und `**` wirken wie in `path`) und `optional`.
Drei Regeln weichen von JSON Schema ab, damit sich eine Regel nicht umgehen
lässt:

- Ein `pattern` muss auf den ganzen Wert passen, nicht nur auf seinen Anfang.
  `.` passt auf einen Zeilenumbruch nur, wo beide Lesarten übereinstimmen:
  Schreiben Sie `(?s)`, wenn es das soll.
- Ein Name unter `args`, `query`, `headers` oder `properties` muss vorhanden
  sein. Ein weggelassenes Argument erfüllt seine Bedingung nicht und überspringt
  nie die Regel. Schreiben Sie `"optional": true`, wenn es fehlen darf; bei einer
  `deny`-Regel verbietet das auch den Aufruf, der es weglässt.
- Ein Schlüsselwort für einen anderen Typ als den des Werts (ein `pattern` auf
  eine Liste) macht das Ergebnis unbestimmt, statt es durchzulassen.

**Was Server unterschiedlich lesen könnten, ist unbestimmt.** Das umfasst einen
JSON-Schlüssel, der doppelt oder auch in anderer Schreibweise vorkommt, einen
Body, der sich nicht lesen lässt (weder JSON noch Formular, anders als mit gzip
oder deflate komprimiert, größer als 1 MiB), eine Query mit `;` oder einem
wiederholten Namen, eine Methoden-Überschreibung (`X-HTTP-Method-Override`,
`X-HTTP-Method`, `X-Method-Override`, `_method`), eine Pfad-Überschreibung
(`X-Original-URL`, `X-Rewrite-URL`), einen Pfad, der nur ohne Beachtung der
Schreibweise oder nur ohne NUL oder Punkte am Ende passt, einen relativen Pfad
unter `path_glob` und einen Wert über 100.000 Zeichen unter `pattern`. Eine
`allow`-Regel braucht einen eindeutigen Treffer, `deny`- und `ask`-Regeln nehmen
auch einen unbestimmten. Was sich nicht beurteilen lässt, erlaubt daher nie eine
Regel. Bei einem Gerät ist unbestimmt, was sich nicht aus sysfs lesen lässt:
ein Knoten, den sysfs nicht kennt, ein Link, der aus `/sys/devices` hinausführt,
ein Attribut, das keine reguläre Datei ist, eine Seriennummer, die kein
druckbares ASCII ist.

**Im Proxy.** HTTP-Regeln gelten für jeden Namen, den eine Anfrage erreichen
kann, wie Regeln von `--allow-http`. Das sind der geprüfte Servername, der
`Host`-Header und die gepinnten Adressen des Hosts, eine nackte IP führt also
nicht an ihnen vorbei. Pfade werden so verglichen, wie Server sie auflösen.
Eine Anfrage, die eine Regel verbietet, bekommt ein `403` mit dem Grund, das der
Agent lesen kann. Ein `ask` hält die Anfrage für eine Rückfrage an (Art `http`:
Methode, URL, Regel und der Anfang des Bodys, geschwärzt); *Allow always* gilt
nur für den Lauf. Mit `--ask off` wird die Anfrage abgewiesen. Wenn eine
Body-Bedingung entscheiden könnte, wird der Body (bis 1 MiB) gelesen, bevor
etwas den Server erreicht. Sobald ein Regelwerk HTTP-Regeln hat, trägt jeder
`http`- und `denied`-Eintrag im Verbindungslog `decision`, `rule` und `ruleset`.
Das Feld `rule` enthält die `id` der Regel; `(unlisted)` für eine Anfrage
außerhalb der allow-Regeln eines Hosts; `(default)`; `(host)` für einen
`Host`-Header, der vom geprüften Namen abweicht; oder `null`, wenn kein
Regelwerk etwas zu sagen hatte. HTTP-Regeln brauchen den Proxy, und ein Host
unter `--no-inspect` kann keine haben: Ein solcher Start wird abgelehnt.

**Im MCP-Relay (dem Gateway).** Das Relay prüft jeden `tools/call`, bevor der
Server ihn sieht. Einen verbotenen Aufruf beantwortet es selbst, mit einem
Fehlerergebnis, das den Grund nennt, und `tools/list` lässt die Werkzeuge weg,
deren Aufrufe alle verboten wären. Das Relay weist JSON-RPC-Batches ab, Zeilen,
die nicht genau eine JSON-Nachricht sind, doppelte Schlüssel, Methodennamen in
anderer Schreibweise und Werkzeugnamen mit anderen Zeichen als
`A-Z a-z 0-9 _ - . : /`. Nachrichten werden neu kodiert weitergegeben, sodass
der Server liest, was geprüft wurde. Jeder Aufruf steht in
`~/.local/state/sandbox-ai/mcp/NAME/mcp-calls.jsonl`, mit seinen Argumenten
(gekürzt und geschwärzt), Entscheidung, Regel, Regelwerk und Antwort, danach
mit seinem Ergebnis (Größe, `is_error`, Dauer). Eine Rückfrage zu einem Aufruf
erscheint in `sandbox-ai approvals` (Lauf `mcp-NAME`) und als
Desktop-Benachrichtigung; bei einem MCP-Server bedeutet `run`: bis der Server
stoppt. Die Regeln eines Servers gehören zu dem, was er ist: Ein Agent mit
anderen Regeln für einen laufenden Server wird abgewiesen, bis
`sandbox-ai mcp stop NAME`.

**Nicht vertrauenswürdige Inhalte: der Taint des Laufs.** Ein Regelwerk
kann Quellen nicht vertrauenswürdiger Inhalte (Antworten von Hosts, Ergebnisse
von MCP-Werkzeugen) unter `"untrusted"` markieren. Eine Regel mit
`"when": {"tainted": true}` gilt erst, wenn solcher Inhalt in den Lauf gelangt
ist:

```json
{
  "version": 1,
  "untrusted": [
    {"id": "web", "host": "*"},
    {"id": "fetch", "server": "fetch", "tool": "*"}
  ],
  "http": [
    {"id": "tainted-writes", "host": "*", "methods": ["POST", "PUT", "PATCH", "DELETE"],
     "when": {"tainted": true}, "decision": "ask"}
  ],
  "mcp": [
    {"id": "tainted-files", "server": "files", "tool": ["write_file", "move_file"],
     "when": {"tainted": true}, "decision": "ask"}
  ]
}
```

Das ist die Regel von CaMeL auf der Ebene von sandbox-ai. sandbox-ai sieht
nicht, wie Daten im Modell fließen; ist nicht vertrauenswürdiger Inhalt einmal
im Kontext des Agenten, kann jede spätere Aktion darin versteckten Anweisungen
folgen. Mit der Datei oben fragt jeder Schreibzugriff auf einen Host außer dem
eigenen Anbieter des Agenten zuerst nach, ebenso jeder Push, denn ein Push ist
ein POST. Dasselbe gilt für die gewählten MCP-Werkzeuge.

- **Der Taint kommt zuerst.** Der Proxy hält den Kopf einer Antwort eines
  solchen Hosts an, bis der Supervisor den Taint festgehalten hat, und das
  MCP-Relay hält ihn fest, bevor es das Ergebnis eines Werkzeugs weitergibt.
  Die nächste Anfrage des Agenten trifft schon auf die Regeln für Läufe mit
  Taint.
- **Quellen** nehmen `"host"` (und wahlweise `"path"`) für Antworten von
  Hosts, `"tool"` (und wahlweise `"server"`) für Ergebnisse von
  MCP-Werkzeugen, oder `"agent"` und `"tool"` für die eigenen Werkzeuge des
  Agenten (seine Hooks, siehe unten). `"host": "*"` markiert jeden Host außer dem Anbieter.
  Inhalt, der kein HTTP ist oder ungeöffnet durchläuft (`--no-inspect`), zählt
  allein nach seinem Host.
- **Der eigene Anbieter des Agenten** (seine Modell-API und Login-Hosts, die
  von `--frontier`, der `--llm`-Server) ist nie eine Quelle und wird nie
  angehalten. Das gilt nur unter seinem geprüften Namen: Der Name des
  Anbieters in einem `Host`-Header an einen anderen Server zählt nicht.
- **Sichtbar:**
  - Eine Rückfrage, die eine solche Regel stellt, nennt den Grund: "asked
    because untrusted content from news.example entered this run at
    10:00:00Z".
  - Das Verbindungslog bekommt ein Ereignis `taint`, und jeder spätere
    Eintrag trägt `tainted`. Die MCP-Aufrufe des Laufs tragen es in
    `mcp-calls.jsonl` im Log-Verzeichnis.
  - `sandbox-ai activity` zeigt das Ereignis, und `sandbox-ai net show`
    zeigt, seit wann der Lauf einen Taint hat und woher.
- **Nur Sie nehmen ihn zurück:** `sandbox-ai net untaint [LAUF]`. Ein neuer
  Lauf beginnt ohne Taint.
- **MCP-Server je Lauf:** Der Agent erreicht jeden MCP-Server über ein Relay
  des Supervisors seiner eigenen Sandbox. Das Relay wendet diese Regeln an,
  stellt ihre Rückfragen über die Rückfragen dieses Laufs und protokolliert
  die Aufrufe dieses Laufs. Die eigenen Sockets der Server sieht der Agent
  nicht.

Grenzen:

- Der Taint ist absichtlich grob: Jeder spätere Schreibzugriff fragt nach, wie
  wenig er auch mit dem Inhalt zu tun hat.
- Inhalt, der in eine Datei geschrieben und in einem neuen Lauf gelesen wird,
  ist dort nicht sichtbar.
- Anfragen auf anderen Verbindungen, die schon unterwegs sind, wenn der Taint
  festgehalten wird, werden nicht angehalten.
- Verfolgt wird nur Inhalt von Hosts und Werkzeugen, die ein Regelwerk
  markiert.

**Die eigenen Werkzeuge des Agenten: seine Hooks.** Regeln unter `"tool"`
gelten für die eigenen Werkzeuge des Agenten, bevor sie laufen: Bash,
Dateiänderungen, WebFetch und Ähnliches.

```json
{
  "version": 1,
  "untrusted": [{"id": "search", "agent": "*", "tool": ["WebSearch", "WebFetch"]}],
  "tool": [
    {"id": "downloads", "tool": "Bash", "args": {"command": {"pattern": "(?s).*\\b(curl|wget)\\b.*"}},
     "decision": "ask"},
    {"id": "edits-in-work", "tool": ["Write", "Edit", "MultiEdit", "apply_patch"],
     "args": {"paths": {"not": {"items": {"path_glob": ["/work/**", "/tmp/**"]}}}}, "decision": "deny"},
    {"id": "docs", "tool": "WebFetch", "args": {"url": {"glob": "https://docs.python.org/*"}},
     "decision": "allow"},
    {"id": "tainted-bash", "tool": "Bash", "when": {"tainted": true}, "decision": "ask"}
  ]
}
```

| Werkzeugregel | Bedeutung |
|---|---|
| `agent` | `claude`, `codex`, `opencode`, eine Liste oder `*` (Standard) |
| `tool` (Pflicht) | Ein Werkzeugname oder ein Muster mit `*`, oder eine Liste |
| `args` | `{"NAME": Bedingung}` für die Eingabe des Werkzeugs |

`"defaults": {"tool": "ask"}` lässt jeden anderen Werkzeugaufruf nachfragen.
Die Regel für Erlaubnislisten und `when` wirken wie bei MCP-Werkzeugen.

- **Werkzeugnamen** sind die von Claude Code:
  - `Bash`, `Read`, `Write`, `Edit`, `MultiEdit`, `Glob`, `Grep`, `LS`,
    `WebFetch`, `WebSearch` und `Task`.
  - `mcp__SERVER__TOOL` für MCP-Werkzeuge.
  - Codex nennt seine Shell `Bash` und seine Dateiänderungen `apply_patch`.
  - Die Namen von OpenCode werden darauf abgebildet (`bash` auf `Bash`,
    `write` auf `Write`, `webfetch` auf `WebFetch`, `patch` auf
    `apply_patch` und das Argument `filePath` auf `file_path`). Eine Regel
    gilt so für alle drei Agenten.
- **Argumente** sind die Eingabe des Werkzeugs, wie der Agent sie schickt:
  `command` bei Bash, `file_path` bei Read, Write und Edit, `url` bei
  WebFetch. sandbox-ai ergänzt `paths`, die Dateien, die ein Aufruf nennt,
  absolut gemacht gegen das Arbeitsverzeichnis des Agenten:
  - `file_path`, `notebook_path` oder `path`;
  - jede Datei eines Codex-Patches;
  - das Arbeitsverzeichnis einer Suche ohne `path`.

  `paths` kann der Agent nicht selbst setzen. Eine Zeichenkette über 256 KiB
  ist den Regeln unbekannt; sie wird nie gekürzt.
- **Nicht vertrauenswürdige Werkzeuge.** Eine Quelle mit `"agent"` und
  `"tool"` markiert die Ergebnisse eines eigenen Werkzeugs des Agenten als
  nicht vertrauenswürdig. Das zählt bei `WebSearch`: Claude Code führt es bei
  Anthropic aus, der Proxy sieht es also nie. Der Lauf bekommt den Taint,
  sobald ein solcher Aufruf erlaubt ist, noch bevor sein Ergebnis da ist.
- **Wie sie eingerichtet werden.** Der Launcher schreibt die Hooks als
  verwaltete Konfiguration des Agenten und bindet sie schreibgeschützt ein:
  - Claude Code: `/etc/claude-code/managed-settings.json` mit
    `allowManagedHooksOnly`. Nur diese Hooks laufen, und das eigene
    `disableAllHooks` des Agenten schaltet sie nicht ab.
  - Codex: `/etc/codex/requirements.toml` mit
    `allow_managed_hooks_only = true` und fest eingeschaltetem Feature
    `hooks`. Eine Konfiguration, die das Feature abschaltet, lässt Codex
    nicht starten. Ein Codex älter als 0.131.0 würde diese Schlüssel
    stillschweigend übergehen, deshalb wird der Start abgelehnt (der
    Supervisor ruft `codex --version` auf).
  - OpenCode: ein Plugin (`tool.execute.before` und `after`), genannt in
    `OPENCODE_CONFIG_CONTENT`.

  Jeder Werkzeugaufruf startet `python3 -I /sandbox/runtime.py
  --internal-hook AGENT EREIGNIS`. Dieser Hook fragt den Supervisor über einen
  Socket, den der Agent erreicht, aber nicht ersetzen kann. Der Supervisor
  wendet die Regeln an und fragt Sie, wo eine Regel `ask` sagt (Art `tool`).
  *Immer erlauben* gilt nur für den Lauf, und mit `--ask off` wird der Aufruf
  abgelehnt. Jeder Aufruf steht in `tools.jsonl` im Log-Verzeichnis, mit
  seiner Eingabe (gekürzt und geschwärzt), Entscheidung, Regel und Regelwerk.
- **Im Zweifel abgelehnt.** Nur eine Antwort `allow` lässt einen Aufruf
  durch. Eine fehlende oder kaputte Antwort lehnt ihn ab, ebenso eine
  Hook-Eingabe, die sich nicht lesen lässt, ein unbekannter Agent oder ein
  unbekanntes Ereignis, oder keine Antwort binnen 110 Sekunden (die Agenten
  warten 120). Ein abgelehnter Aufruf erreicht den Agenten als Fehler, der mit
  `sandbox-ai:` beginnt und den Grund nennt.
- **Nur wenn nötig.** Die Hooks werden nur eingerichtet, wenn ein Regelwerk
  Werkzeugregeln hat, einen `tool`-Standard außer `allow` oder ein nicht
  vertrauenswürdiges Werkzeug. Hooks aus Plugins und den eigenen
  Einstellungen des Agenten laufen dann nicht (Claude Code und Codex); Hooks
  aus Kits laufen nach den Regel-Hooks, sobald Sie sie freigegeben haben
  ([Hooks in Kits](#hooks-in-kits)). Mit
  `--binary` lesen Claude Code und Codex die verwaltete Konfiguration
  weiterhin, OpenCode bekommt aber kein Plugin.

**Eine Leitplanke, keine Grenze.** Die Hooks sehen den Aufruf, den der Agent
macht, nicht, was das gestartete Programm tut:

- `bash -c "$(echo Y3VybCB4 | base64 -d)"`, ein vorher geschriebenes Skript
  oder `python3 -c` kommt an einer Regel für `curl` vorbei.
- Ein Programm, das aus Bash läuft, schreibt Dateien, die eine `paths`-Regel
  für Write und Edit ablehnt.
- Einen zweiten Agenten, der aus Bash mit anderen Einstellungen startet,
  sehen sie nicht.

Regeln für Bash-Befehle fangen Fehler und einfache Prompt-Injektionen ab,
nicht einen Agenten, der sie umgeht. Die Grenze liegt, wo sie war: bei den
Netzwerkoptionen, beim Proxy mit seinen HTTP-Regeln, beim MCP-Relay und bei
den Einbindungen.

Grenzen: Die Regeln sehen das HTTP/1.1, das der Proxy öffnet. Sie sehen keine
WebSocket-Nachrichten nach einem Upgrade, und sie sehen kein HTTP, das der Proxy
auf anderen Ports als 80 und 443 zu Hosts ohne benannte Regeln nicht als
solches erkennt (etwa eine Anfragezeile mit kleingeschriebener Methode).
Absichtlich kodierte Daten (base64, Verschlüsselung) passieren jedes Muster.
MCP-Regeln decken `tools/call` ab, nicht `resources/read`, `prompts/get` oder
eigene Methoden eines Servers. Ein Server, der in einer eigenen Sandbox läuft,
teilt deren Benutzer mit dem Relay; ein Werkzeug, das beliebigen Code ausführt
(eine Shell), könnte das Relay ersetzen: Verbieten Sie solche Werkzeuge. Regeln
für die eingebauten Werkzeuge der Agenten (Bash, WebFetch) gehören noch nicht
dazu (siehe TODO.md). Geräteregeln richten sich nach dem, was ein Gerät über
sich angibt (siehe [Hardware](#hardware-geräte-und-gpus)); sie sehen nicht, was
der Agent mit einem erhaltenen Gerät tut, und ein als Ganzes übergebenes
Verzeichnis (`/dev/bus/usb`) ist für sie ein Gerät.

## Köderdateien (Canaries)

Ein Canary ist ein Köder: eine Datei, die wie eine Zugangsdatei aussieht und
für einen Lauf in die Sandbox gelegt wird. Nichts Legitimes braucht sie; wer
sie öffnet und vor allem wer ihren Inhalt irgendwohin schickt, ist also
interessant (eine Prompt Injection, die den Agenten Schlüssel sammeln lässt,
das Installationsskript eines bösartigen Pakets, ein Werkzeug, das Ihr HOME
hochlädt).

```bash
sandbox-ai claude                                  # credentials und home, der Standard
sandbox-ai claude --canaries credentials,workspace # dazu eine .env in jedem Workspace
sandbox-ai claude --canaries all                   # credentials, home und workspace
sandbox-ai claude --no-canaries
sandbox-ai canaries list                        # Läufe mit Canaries
sandbox-ai canaries list LAUF [--tokens] [--json] # ihre Dateien, Tokens und was geschah
sandbox-ai canaries log [LAUF] [--json]         # jedes Ereignis: durch welchen Prozess, neben dem Zug des Agenten
sandbox-ai canaries resume LAUF                 # eine von einem Canary pausierte Sandbox fortsetzen
```

| Kategorie | Dateien | Standard |
|---|---|---|
| `credentials` | `~/.aws/credentials` (ein benanntes Profil), `~/.config/gcloud/legacy_credentials/KONTO/adc.json`, `~/.ssh/id_ed25519_{prod,deploy,backup}`, `~/.config/gh/hosts.yml`, `~/.netrc`, `~/.git-credentials` | an, im HOME der Sandbox |
| `home` | `~/Documents/passwords.txt`, `~/.kube/config` | an, im HOME der Sandbox |
| `workspace` | `.env` in jedem Workspace | aus: es verändert Ihr Projektverzeichnis |

**Die Tokens.** Jeder Lauf (jedes behaltene HOME) bekommt eigene zufällige
Tokens in der Form echter, die kein Dienst annimmt: AWS-Schlüssel in der Form
von AWS' dokumentiertem Beispiel (`AKIA…EXAMPLE`, ein Secret auf
`EXAMPLEKEY`), ein GitHub-Token (`ghp_…`) mit falscher Prüfsumme, ein
GitLab-Token, ein gcloud-Refresh-Token, zufällige Passwörter und Hosts unter
`.internal` (ein für private Nutzung reservierter Name), die die Sandbox nicht
auflösen kann. Der SSH-Schlüssel hat die Hülle eines privaten Schlüssels,
ergibt dekodiert aber keinen. Es sind nicht die Dateien, die Werkzeuge von
selbst benutzen (ein benanntes AWS-Profil, `id_ed25519_prod` statt
`id_ed25519`, ein gh-Login für einen Host unter `.internal`), also schicken
`aws`, `ssh`, `gh` oder `git` nie von sich aus einen Canary. Nichts wird über
eine vorhandene Datei gelegt, unter einen Mount (`--mount`, Kits) oder durch
einen Link (ein Agent kann einen in einem behaltenen HOME oder Workspace
hinterlassen haben); die Dateien haben `0600` in `0700`-Verzeichnissen und
sind Wochen zurückdatiert. Die `.env` im Workspace verwendet Variablennamen,
die kein Framework liest (`PROD_DATABASE_URL`, `DEPLOY_API_KEY`).

**Sie können sie von Ihren eigenen Dateien unterscheiden:** Es gibt sie nur
im HOME der Sandbox (einem temporären oder einem behaltenen: `--state-dir`,
Sessions) und, mit `workspace`, als `.env` in einem Workspace, der keine
hatte. `sandbox-ai canaries list LAUF` nennt jeden mit seinem Pfad in der
Sandbox und auf dem Host; `--tokens` zeigt die Tokens. Ein Workspace-Canary
steht für die Dauer des Laufs in `.git/info/exclude` (`git add -A` lässt ihn
aus) und verschwindet mit dieser Zeile, wenn der Lauf endet; nach einem
abgebrochenen Lauf räumt der nächste Start auf. Die Canaries eines
Wegwerf-HOME gehen mit ihm; ein behaltenes HOME behält sie von Lauf zu Lauf mit
denselben Tokens (ein Canary, den Sie oder der Agent verändert haben, ist
keiner mehr und bleibt, wie er ist; `--no-canaries` entfernt die
unveränderten). Ihr Verzeichnis liegt außerhalb des HOME, in
`~/.local/state/sandbox-ai/canaries/`.

**Was erkannt wird.**

| Signal | Gesehen von | Ereignis |
|---|---|---|
| Ein Canary wird geöffnet oder gelesen | dem Supervisor (inotify `IN_OPEN`/`IN_ACCESS` auf der Datei) | `read` |
| Ein Canary wird geändert, umbenannt, gelöscht oder ersetzt | dem Supervisor (inotify auf Datei und Verzeichnis) | `modify`, `move`, `delete`, `replace` |
| Ein Token in URL, Headern oder Body einer Anfrage: im Klartext, URL-kodiert (beide Schreibweisen), JSON-maskiert, hex, base64 oder base64url (an jeder Position), auch in einem gzip- oder deflate-Body, auch über Teile eines Body hinweg | dem Verbindungs-Proxy, bevor die Anfrage (oder der Rest ihres Body) den Server erreicht | `send` |
| Ein Token im Namen, den ein TLS-Client verlangt (SNI), oder im `Host`-Header (Klartext, hex, base32, base64url, über Labels hinweg) | dem Proxy | `send` |
| Ein Token in einer DNS-Anfrage, die die Sandbox nicht erlaubt | dem DNS-Stub des Supervisors | `send` |
| Ein Token in gepushten Commits (Dateien, Nachrichten, Namen), zu jedem Remote | der Push-Prüfung | `send` |
| Ein Token in einer Anfrage an den Modell-Anbieter des Agenten | dem Proxy | `context`: der Agent hat einen Canary in sein Gespräch gelesen; nie als Abfluss blockiert |

Liste und Tokens der Canaries kommen nie in den Teil der Sandbox, den der
Agent lesen kann: Der Supervisor bekommt die Pfade und die DNS-Formen im
Steuerverzeichnis des Laufs, Proxy und Push-Prüfung die Tokens im Verzeichnis
des Proxys; Firejail verbirgt beide vor dem Agenten. Nichts davon steht in
seiner Umgebung oder in `/sandbox/session`.

**Was folgt**, entscheiden die [Regelwerke](#regelwerke) (das strengste
gewinnt, die Richtlinie des Administrators eingeschlossen):

```json
{"version": 1, "canary": {"on_read": "ask", "on_send": "stop"}}
```

| Schlüssel | Werte | Standard |
|---|---|---|
| `on_read` (ein `read` eines meldenden Canary, und `context`) | `report` (Benachrichtigung), `ask` (Sandbox pausieren und fragen: weiter oder stoppen), `pause`, `stop` | `report` |
| `on_send` (ein Token verlässt die Sandbox) | `report` (durchlassen, melden), `block` (Anfrage abweisen, melden), `pause` (abweisen und pausieren), `stop` (abweisen und die Sandbox stoppen) | `stop` |

Änderungen (`modify`, `move`, `delete`, `replace`) werden immer gemeldet, nie
mit einer Aktion beantwortet (`git clean -x` löscht eine Workspace-`.env`).
Lesezugriffe auf `~/.netrc` und `~/.config/gh/hosts.yml` werden nur
protokolliert: curl, pip, requests, git und gh öffnen sie von selbst; dass ihre
Tokens die Sandbox verlassen, zählt trotzdem. Jedes Ereignis steht in
`canaries.jsonl` im Log-Verzeichnis und in `sandbox-ai canaries list LAUF`;
eine Meldung erscheint als Desktop-Benachrichtigung (einmal pro Minute je
Canary und Art), in `sandbox-ai approvals history`, in der App und im
[Aktivitätsverlauf](#aktivität-der-agenten) (Einträge der Art `canary`, als
Rückfragen gezeigt); eine abgewiesene Anfrage ist ein `denied`-Eintrag mit
`canary` im Verbindungslog. `ask` ist eine Rückfrage der Art `canary`
(`sandbox-ai approvals allow ID` setzt fort, `deny ID` stoppt; die
Benachrichtigung hat Knöpfe); ohne Antwort nach 5 Minuten bleibt die Sandbox
pausiert. Eine Pause friert den Podman-Pod oder jeden Prozess der
Bubblewrap-Sandbox ein; `sandbox-ai canaries resume LAUF` (die Run-ID oder der
Name des Starts; `sandbox-ai resume NAME` geht auch) setzt fort, `sandbox-ai stop NAME` beendet.

**Das Canary-Log.** `sandbox-ai canaries log [LAUF] [--json]` (in der App: Aktivität, Reiter Canaries; in der
Weboberfläche: Canaries) zeigt jedes Ereignis mit Zeit, Canary, Pfad und Aktion und wer es war: bei einem
Token, das die Sandbox verließ, der Prozess, den Proxy oder DNS-Stub fanden; bei einem Dateiereignis die
Prozesse, die der Supervisor gleich nach der inotify-Meldung mit der Datei offen fand (inotify selbst nennt
niemanden: wer die Datei vor der Suche schon geöffnet, gelesen und geschlossen hat, steht ehrlich als
`unknown` da). Jedes Ereignis ist mit dem Tool-Aufruf des Agenten im selben Zeitfenster (zuerst dem, der
die Datei nennt), seinem Prompt-Zug und den Anfragen an den Modellanbieter kurz davor und danach verknüpft
(der, die das Token trug, falls eine), über die IDs ihrer Einträge im
[Aktivitätsverlauf](#aktivität-der-agenten) (oder Datei und Offset im Verbindungslog, wenn der Verlauf eine
Anfrage nur mitzählte). Alles ist geschwärzt wie im Aktivitätslog; die Tokens des Laufs erscheinen nie.

**Auswahl.** `--canaries KATEGORIEN` / `--no-canaries` auf der Befehlszeile
oder in `"options"` der Projektdatei; der Schlüssel `"canaries":
["credentials", "workspace"]` (oder `false`) der Projektdatei;
`"canaries"` in `~/.config/sandbox-ai/defaults.json` (gleiche Form) für jeden
Start, an dem keins davon wählt. Die Richtlinie des Administrators kann sie
verlangen (`"require_canaries": true` für die Standardkategorien, oder eine
Liste) und `--no-canaries` verbieten; ihr `rules` kann den Abschnitt `canary`
setzen. Probes und Sandboxen von MCP-Servern bekommen keine.

**Sicherheitsmodi** zählen wie ein Regelwerk (das strengste gewinnt: ein
Regelwerk kann strenger sein als der Modus, nie lockerer) und legen
mindestens ihre Kategorien; `--dry-run` zeigt das Ergebnis unter `canaries`,
mit `decided_by`:

| Modus | Kategorien | `on_read` | `on_send` |
|---|---|---|---|
| `lax` | `credentials`, `home` | `report` | `report` |
| `low`, `medium` | `credentials`, `home` (die Vorgaben des Launchers) | `report` | `stop` |
| `high` | `credentials`, `home`; `--no-canaries` abgewiesen | `ask` | `stop` |
| `paranoid` | `credentials`, `home`; `--no-canaries` abgewiesen (Workspaces sind nur lesbar, also keine Workspace-Köder) | `stop` | `stop` |

**Grenzen.** inotify sagt nicht, welcher Prozess eine Datei geöffnet hat, und
sieht auch Zugriffe vom Host (eine Sicherung, ein Indexer, Ihr Editor auf der
Workspace-`.env`). Eine Laufzeit, die `.env` von selbst lädt (Bun, also
OpenCode; dotenv in Ihrer Anwendung), liest den Workspace-Canary beim Start.
Was der Proxy nicht öffnet, wird nicht durchsucht: Hosts unter `--no-inspect`
(nur ihr TLS-Name), TCP zu Zielen von `--allow-tcp`/`--allow`, SSH,
WebSocket-Nachrichten nach einem Upgrade und DNS mit `--dns` (es geht direkt
an die Resolver). Ein Token, in Stücke geteilt und in getrennten Anfragen
geschickt, verschlüsselt, anders komprimiert oder als base64 mit
Zeilenumbrüchen, kommt durch; ebenso ein von Hand umgeschriebenes. Pushes
durchsucht die Push-Prüfung, die `--privacy-check` ungleich `off` braucht.
Der DNS-Stub läuft mit einem Log-Verzeichnis (Standard, außer mit
`--no-proxy`).

## Geheimnisse

`sandbox-ai secrets` bewahrt API-Schlüssel und Tokens im System-Schlüsselbund
auf (Secret Service: GNOME Keyring, KWallet, KeePassXC; über `secret-tool`),
oder, wenn es keinen gibt, in privaten Dateien (`0600`) unter
`~/.local/share/sandbox-ai/secrets` (`SANDBOX_AI_SECRETS=file` erzwingt die
Dateien).

```sh
sandbox-ai secrets set OPENROUTER_API_KEY        # Wert von stdin, nie als Argument
sandbox-ai secrets set GH_PUSH --from-env GH_TOKEN
sandbox-ai secrets list                          # Namen und Ablageort, nie Werte
sandbox-ai claude --secret-env OPENROUTER_API_KEY    # der Agent bekommt die Variable
sandbox-ai claude --secret-env API_KEY=OPENROUTER_API_KEY --git-token-secret https://github.com=GH_PUSH
```

Eine Sandbox bekommt ein Geheimnis nur, wenn es genannt wird:
`--secret-env NAME[=GEHEIMNIS]` (eine Umgebungsvariable für den Agenten),
`--api-key HOST=NAME[=GEHEIMNIS]` (siehe unten), `--git-token-secret
URL=GEHEIMNIS` (wie `--git-token-env`) oder die Variable eines MCP-Servers.
Werte werden nie ausgegeben, weder im Dry-Run-Plan noch in Logs (der Proxy
schwärzt sie in aufgezeichneten Anfragen wie jede Anmeldung).

**API-Schlüssel, die außerhalb der Sandbox bleiben.** `--api-key
HOST=NAME[=GEHEIMNIS]` gibt dem Agenten die Variable NAME mit einem Platzhalter
(`sandbox-ai-api-key-placeholder-NAME`), nicht den Schlüssel. Der
Verbindungs-Proxy setzt das Geheimnis GEHEIMNIS (Standard: NAME) erst auf der
Leitung an die Stelle des Platzhalters, und nur in einem Anmelde-Header
(`Authorization`, `X-API-Key`, `API-Key`) einer HTTPS-Anfrage an HOST auf Port
443, dessen Zertifikat er geprüft hat. Überall sonst (ein anderer Host, reines
http, ein Body, ein anderer Header) bleibt der Platzhalter ein Platzhalter.
Eine Anfrage, deren `Host`-Header einen anderen Server nennt als den geprüften
(Frontends leiten danach weiter, und ein Zertifikat dient vielen Mandanten),
lehnt der Proxy mit einem 403 `sandbox-ai: ...` ab und protokolliert sie; das
gilt für jede Anmeldung, die der Proxy einsetzt (`--llm`, `--publish`,
Git-Logins). Die Option öffnet HOST (TCP/443), braucht den Proxy und lehnt `--no-inspect
HOST` ab. Wie der Schlüssel von `--llm` steht er in keinem Log; eine Antwort
erreicht den Agenten so, wie der Server sie schickt: Geben Sie keinem Server
einen Schlüssel, der Anmeldedaten zurückspiegelt.

```bash
sandbox-ai secrets set DEVTO_API_KEY
sandbox-ai claude --api-key dev.to=DEVTO_API_KEY \
    --api-key gql.hashnode.com=HASHNODE_TOKEN    # curl -H "api-key: $DEVTO_API_KEY" https://dev.to/api/articles
```

## Ressourcenlimits

Jede Sandbox läuft mit Limits für Speicher, CPU und Prozesse (während des Laufs
ändern Sie sie mit `sandbox-ai limits [LAUF] --memory 16g --cpus 4 --pids
none`, das die systemd-Unit der Sandbox setzt oder, wo delegiert, ihre
cgroup-Dateien):

| Option | Standard | `none` |
|---|---|---|
| `-me`, `--memory GRÖSSE` | `8g` | kein Speicherlimit |
| `-cu`, `--cpus N` | die Hälfte der CPUs des Hosts | kein CPU-Limit |
| `-pi`, `--pids N` | `4096` | kein Prozesslimit |

Podman gibt sie an den Container weiter (`--memory`, `--cpus`,
`--pids-limit`). Bubblewrap läuft in einem vorübergehenden systemd-Scope
(`systemd-run --user --scope`). Beides braucht eine systemd-Benutzersitzung
mit delegierten cgroup-Controllern, wie auf jedem Desktop; ohne sie (in einem
Container, auf manchen Servern) startet die Sandbox ohne Limits und sagt das.

**Speicherwächter.** Ist das Speicherlimit voll, beendet der Kernel einen
Prozess, und ist das der Supervisor der Sandbox, endet der ganze Lauf. Deshalb
beobachtet der Launcher die cgroup der Sandbox vom Host aus, solange sie läuft
(die Sandbox selbst sieht sie nicht):

- bei 85 % sagt er es Ihnen (Desktop-Benachrichtigung und `memory.jsonl` im
  Log-Verzeichnis) und dem Agenten: `/sandbox/session/memory.json` nennt
  Verbrauch und Limit, wie viel die Dateien in seinem `/tmp` und HOME belegen,
  und einen Rat;
- vom Start an setzt er `memory.high` auf 92 % des Limits: darüber gewinnt der
  Kernel Speicher zurück und bremst die Anforderungen der Sandbox, statt sie das
  Limit erreichen zu lassen, wo sein OOM-Killer vor dem nächsten Blick einen
  Prozess beenden würde (einige Gigabyte binnen zwei Sekunden); ein mit
  `sandbox-ai limits --memory` geändertes Limit gilt mit;
- bei 95 %, oder wenn ihre Prozesse auf Speicher warten (`memory.pressure`: alle
  zusammen 10 % der letzten 10 Sekunden), beendet er den Prozess mit dem meisten
  Speicher, der weder zu sandbox-ai noch zum Agenten selbst gehört (meist ein
  Testlauf oder ein Build): SIGTERM, nach 3 Sekunden SIGKILL; der Agent sieht
  seinen Befehl scheitern, der Lauf geht weiter;
- nach dem Lauf sagt er, was er beendet hat und ob der Kernel trotzdem Prozesse
  beendet hat (dann mit `--memory 16g` starten oder weniger gleichzeitig tun).

Dateien in `/tmp` und `/var/tmp` (und ein HOME, das nicht aufbewahrt wird)
liegen im Speicher (tmpfs) und zählen zum Limit; mit Limit fassen `/tmp` und
`/var/tmp` je höchstens die Hälfte davon, ein volles `/tmp` ist also ein Fehler,
den der Agent sieht („No space left on device“), nicht das Ende des Laufs. Seit
0.1.36 sind die Prozesse des Agenten außerdem die erste Wahl des Kernels
(`oom_score_adj` 300 über dem des Supervisors). Der Wächter braucht eine lokale
Engine unter Linux (nicht `--podman-url`).

## Richtlinie des Administrators

Ein Administrator kann festlegen, was niemand auf dem Rechner wählen darf, in
`/etc/sandbox-ai/policy.json` (root gehörend, nur für root schreibbar, in einem
ebensolchen Verzeichnis; eine kaputte oder unsichere Datei stoppt jeden Start):

```json
{
  "version": 1,
  "forbid": ["--egress-all-allow", "--no-proxy", "--privacy-check=off", "--ask=off"],
  "backends": ["podman"],
  "max_approval_scope": "run",
  "min_mode": "high",
  "allow_within": ["10.0.0.0/8"],
  "allow_hosts": ["pypi.org", "files.pythonhosted.org", "*.corp.example"],
  "mcp_network": "none",
  "subnet": "10.200.0.0/24",
  "subnet_within": ["10.200.0.0/16"],
  "require_vpn": ["corp"],
  "rules": {"version": 1, "http": [{"id": "writes", "host": "*", "methods": ["POST", "PUT", "PATCH", "DELETE"],
                                    "decision": "ask"}]},
  "require_signed_updates": true,
  "require_canaries": ["credentials", "workspace"],
  "forbid_keep": true,
  "message": "siehe Entwicklerhandbuch, Abschnitt 4"
}
```

`forbid` nennt Optionen (`--name`, oder `--name=wert` für einen Wert); sie
werden abgelehnt, wie auch immer sie gewählt wurden: auf der Befehlszeile, als
Kurzform, in einer vertrauten Projektdatei, über ein Profil oder eine Option, die
sie einschließt (`--nameserver` und `--egress-all-allow` schließen `--dns` ein),
oder in der Definition eines MCP-Servers. `backends` beschränkt die
Backends. `max_approval_scope` begrenzt Antworten auf Rückfragen (`once`, `run`
oder `always`); unterhalb von `always` wird nichts für ein Projekt gemerkt, und
gemerkte Freigaben werden nicht angewandt. `min_mode` ist der mildeste
[Sicherheitsmodus](#sicherheitsmodi), den ein Start haben darf: ein Modus
darunter wird abgelehnt, ob über `--mode`, die Projektdatei oder
`defaults.json` gewählt, und ohne Wahl gilt `min_mode` selbst (wenn es strenger
als `medium` ist); auch MCP-Server-Sandboxen halten sich daran. `allow_within` ist eine Obergrenze
fürs Netz: Jedes `--allow`, `--allow-tcp`, jeder Resolver (`--dns`,
`--nameserver`), jeder Host-Port (als `127.0.0.1`), jede Freigabe und jedes
`sandbox-ai net allow` muss darin liegen, und `--egress-all-allow` wird
abgelehnt; benannte Hosts (`--allow-host`, Presets, `--frontier`) müssen in
`allow_hosts` stehen, außer dem eigenen Anbieter des Agenten. Gemerkte Freigaben
außerhalb davon werden nicht angewandt.
Änderungen während eines Laufs (`sandbox-ai net allow`) unterliegen `forbid` und
der Obergrenze ebenso: Der Supervisor der Sandbox prüft sie. `mcp_network: none`
(von `allow_within` eingeschlossen) lehnt MCP-Image-Server mit Netz ab.
`require_vpn` ist `true` (jede Sandbox und jeder MCP-Server mit Netz braucht
`--vpn`, und eine fehlgeschlagene Prüfung stoppt immer) oder eine Liste der
erlaubten [VPN](#vpn)-Profile; `"forbid": ["--vpn"]` verbietet VPNs,
`"forbid": ["--vpn-check=warn"]` nur den Warnmodus.
`subnet` und `subnet6` sind das eigene Netz der Sandbox (`--subnet`,
`--subnet6`) für Starts, bei denen weder eine Option noch eine Projektdatei
noch `~/.config/sandbox-ai/defaults.json` eines gewählt hat. `subnet_within`
ist ihre Obergrenze: Für jede IP-Version, die es nennt, muss das eigene Netz
der Sandbox darin liegen, egal wer es gewählt hat; unter Bubblewrap ist das
das Netz von slirp4netns (`10.0.2.0/24`, wenn nichts gewählt ist, `fd00::/64`
immer). Es braucht `subnet` (und `subnet6` für IPv6-Einträge) als
Voreinstellung darin, weil Podman der Sandbox sonst die Adresse des Hosts gibt.
Mit `--network` entscheidet das Podman-Netz; verbiete `--network`, damit die
Obergrenze auch dort gilt. `forbid: ["--subnet"]` hält Benutzer von der Wahl ab
(ein Wert aus `defaults.json` zählt als Wahl); das `subnet` der Richtlinie
selbst gilt weiter. `rules` ist
ein [Regelwerk](#regelwerke), das jeder Lauf als erstes anwendet; kein anderes
Regelwerk kann es erweitern, und ein kaputtes stoppt jeden Start; sein Abschnitt `canary` legt fest, was
mindestens auf das Lesen eines Canary und einen abfließenden Token folgt. `require_canaries` (`true`: die
Standardkategorien, oder eine Liste von Kategorien) gibt jeder Agenten-Sandbox
[Köderdateien](#köderdateien-canaries) und weist `--no-canaries` ab. `require_signed_updates` lässt `install.sh` und
`sandbox-ai update` nur Releases annehmen, die ein vertrauter Schlüssel
signiert hat ([signierte Updates](#installation-und-updates)). `forbid_keep: true`
lässt jeden Start sein HOME am Ende verwerfen, wie `--no-keep`: `--keep` (und
`"keep": true` einer Projektdatei) wird mit Hinweis ignoriert, `--session`
(per Definition eine aufbewahrte Session) abgelehnt, und frühere aufbewahrte
Starts lassen sich nicht zurückholen (`sandbox-ai resume` lehnt ab; `delete`
entfernt sie). `--state-dir` ist nicht erfasst: Die eigenen Logins, Plugins und
MCP-Server von sandbox-ai nutzen es; es gehört in `forbid`, wenn Benutzer kein
eigenes HOME wählen sollen. Die Richtlinie bindet sandbox-ai, wie
es installiert ist; einen Benutzer, der eine eigene, veränderte Kopie startet,
hält sie nicht auf: Sie schützt vor Bequemlichkeit, Projektdateien und
Versehen, nicht vor den Benutzern des Rechners selbst.

## Weboberfläche

`sandbox-ai web` ist das Gegenstück der Desktop-App im Browser, mit denselben
Themen: Ein Dienst auf einer Maschine verwaltet sie und jeden Host von
[`sandbox-ai hosts`](#andere-hosts) über SSH. Starts und Sitzungen (attach in
einem Terminal der Seite, starten, pausieren, fortsetzen, stoppen, löschen),
Rückfragen (mit einer Marke für offene Fragen), den Doctor, die Logs,
LLM-Server (hinzufügen, prüfen, Modelle, entfernen), die Standards jedes Hosts
(die Telemetrie der Agenten, den Sicherheitsmodus, die Köderdateien) und die
Hosts selbst. Die Seite Terminals zeigt jeden Start, der auf dem Host läuft,
gleich wie er begann (ein Terminal, die Desktop-App, die Seite, eine andere
Maschine): Ein Klick verbindet einen Start in tmux (ein Tab je Start); ein
Start im Vordergrund (`--no-tmux`, eine Aufgabe, kein Terminal) hat kein
Terminal zum Verbinden, und die Seite sagt, warum (`attach_why` in
`list --json`).

```bash
sandbox-ai web user add admin      # ein Passwort (12+ Zeichen) und eine TOTP-App (die otpauth-URI wird einmal gezeigt)
sandbox-ai web vendor              # xterm.js für die Terminals (gepinnt, gegen sein SHA-512 geprüft)
sandbox-ai web serve               # https://localhost:8443/ (der Fingerabdruck des Zertifikats wird ausgegeben)
sandbox-ai web serve --listen 0.0.0.0:8443 --hostname box.lan   # aus Ihrem Netz erreichbar
```

- **Nur HTTPS**, mit einem eigenen Zertifikat, beim ersten Start erzeugt (oder
  `--cert`/`--key`); `127.0.0.1`, solange `--listen` nichts anderes sagt. Von
  woanders lieber über einen SSH-Tunnel (`ssh -L 8443:localhost:8443 box`) oder
  einen eigenen Reverse Proxy.
- **Anmeldung: Passwort und TOTP.** Das Passwort liegt als scrypt-Hash in
  `~/.config/sandbox-ai/web/users.json` (Modus 0600), ein TOTP-Code gilt einmal.
  Fehlversuche werden je Adresse sowie je Adresse und Benutzer langsamer (sich
  verdoppelnde Wartezeiten); viele Fehlversuche für einen Benutzer bremsen jede
  Adresse, die erneut scheitert, sperren den Inhaber aber nie aus: richtiges
  Passwort und Code kommen weiter herein. Es zählt nur die Adresse der Verbindung
  (`X-Forwarded-For` wird nie gelesen).
- **Sitzungen** leben im Speicher: 30 Minuten ohne Nutzung (die eigenen Abfragen
  der Seite zählen nicht als Nutzung, ein vergessener Tab meldet sich also ab),
  höchstens 12 Stunden. Das Cookie ist `__Host-`, `HttpOnly`, `Secure`, `SameSite=Strict`.
  Jede Änderung braucht das CSRF-Token der Sitzung, eine Anfrage derselben
  Herkunft und JSON; der `Host`-Header muss einer der Namen des Dienstes sein
  (kein DNS-Rebinding). Die Seite läuft mit einer strengen Content Security
  Policy und zeigt Text vom Server nur als Text. Höchstens 64 Verbindungen
  zugleich; eine ohne Sitzung wird 10 Sekunden gehalten.
- **Jede Aktion ist ein sandbox-ai-Befehl**, jedes Argument einzeln
  gequotet, auf dieser Maschine oder über SSH: Der Browser kann keinen
  Shell-Befehl schicken. Terminals sind ein WebSocket in ein pty, in dem dort
  `sandbox-ai attach` oder ein Start läuft (`ssh -tt` für einen Host); den Tab
  schließen löst nur, der Start läuft weiter.
- **Ein Audit-Log** (`~/.local/state/sandbox-ai/web/audit.jsonl`) hält
  Anmeldungen, Fehlversuche, Änderungen und Terminals fest, ohne Geheimnisse.
- **Keine Sandbox erreicht ihn:** Der Launcher lehnt `--allow-host-port` für den
  Port eines laufenden Webdienstes ab.

`sandbox-ai defaults show|set|unset SCHLÜSSEL WERT` ändert
`~/.config/sandbox-ai/defaults.json`, ohne sie zu bearbeiten (jeder Wert vom
Launcher geprüft, bevor die Datei ersetzt wird); die Weboberfläche setzt die
Standards anderer Hosts darüber.

## Desktop-App

[sandbox-ai-app](https://github.com/c0decave/sandbox-ai-app) ist eine
grafische Oberfläche (Python, Qt) für alles oben: Sandboxen mit allen Optionen
starten, die laufenden sehen, verfolgen, was ihre Agenten tun und welche
Verbindungen sie aufbauen, Rückfragen
beantworten, ihr Netzwerk ändern, MCP-Server, Geheimnisse, Logins und Images
verwalten, die Dateien von HOME und der Workspaces durchsuchen und in
Terminals der Sandboxen arbeiten. Sie ruft nur die `sandbox-ai`-Befehle auf und
liest deren Dateien; die Sandboxen hängen nicht von ihr ab.

`install.sh` installiert sie mit sandbox-ai (aus einem Release oder aus einem
Checkout mit `projects/sandbox-ai-app`) als Befehl `sandbox-ai-app` und als
Eintrag „sandbox-ai“ im Desktop-Menü. Sie braucht PySide6 (Qt für Python):
Fehlt es `python3`, legt der Installer `PySide6-Essentials` von PyPI in eine
eigene Umgebung der App (`~/.local/share/sandbox-ai/app-env`), nie ins Python
des Systems.

```bash
./install.sh --from .                   # sandbox-ai und die App
sandbox-ai-app                          # starten (oder über das Desktop-Menü); aus einem Terminal läuft sie im Hintergrund
sandbox-ai-app --foreground             # im Terminal bleiben (--debug: dazu die Debug-Ausgabe von Qt)
./install.sh --from . --no-app-terminal # ohne Terminal-Tabs (die brauchen QtWebEngine und xterm.js)
./install.sh --from . --no-app-deps     # PySide6 nie herunterladen (python3-pyside6 der Distribution)
./install.sh --from . --no-app          # ohne die App
./sandbox-ai-app                        # aus einem Checkout, ohne Installation
```

Qt selbst braucht die OpenGL/EGL-, xkbcommon- und fontconfig-Bibliotheken des
Systems (Debian/Ubuntu: `libegl1 libgl1 libxkbcommon0 libfontconfig1`); der
Befehl sagt es, wenn sie fehlen. Die Seiten der App beschreibt
[projects/sandbox-ai-app/README.md](projects/sandbox-ai-app/README.md).

## Optionsübersicht

`sandbox-ai codex`, `sandbox-ai claude` und `sandbox-ai opencode` haben alle folgenden
Optionen gemeinsam; `./sandbox-ai claude --help` zeigt sie ebenfalls. Argumente
nach `--` gehen unverändert an den Agenten. Mit *mehrfach* markierte Optionen
dürfen wiederholt werden.

Jede Option hat eine Kurzform. Einbuchstabige lassen sich bündeln und nehmen
ihren Wert direkt: `sandbox-ai claude -Dn -w~/src/app` ist
`--dangerously-skip-permissions --dry-run --workspace ~/src/app`.
Zweibuchstabige wie `-gl` (`--allow-gitlab`) sind eigene Optionen, keine zwei
gebündelten Buchstaben. Auch Projektdateien und MCP-Definitionen dürfen
Kurzformen enthalten. Die anderen Befehle haben ebenfalls Kurzformen (`-V`
Version, `-j` JSON, `-y` Ja, `-b` Backend; siehe deren `--help`).

### Die wichtigsten Optionen

| Option | Zweck |
|---|---|
| `-w`, `--workspace DIR` | Projekt unter `/work` (Standard: aktuelles Verzeichnis) |
| `-mo`, `--mode MODE` | [Sicherheitsmodus](#sicherheitsmodi): `lax`, `low`, `medium` (Vorgabe), `high` oder `paranoid`; Optionen dürfen ihn nur enger machen |
| `-ml`, `--mode-at-least MODE` | Eine Untergrenze: der Modus MODE nur, wenn er strenger ist als der aus den Vorgaben des Projekts oder deinen (das `--mode` eines Presets, die sandbox-ai-Presets der App) |
| `-N`, `--name NAME`, `-nkp`, `--no-keep` | Den Start benennen (Vorgabe `AGENT-PROJEKT-xxxx`); sein HOME am Ende verwerfen statt aufbewahren |
| `-s`, `--session NAME` | Aufbewahrte, benannte Session; Verwaltung mit `sandbox-ai list\|attach\|stop\|resume` |
| `-D`, `--dangerously-skip-permissions` | Agent ohne Rückfragen; die Sandbox bleibt in Kraft |
| `-g`, `--allow-github`, `-y`, `--allow-pypi`, `-A`, `--allow-host HOST` | Einzelne HTTPS-Ziele öffnen |
| `-P`, `--allow-host-port PORT` | Dienst am Loopback des Hosts erreichen, etwa einen lokalen Git-Server |
| `-ge`, `--git-token-env URL=NAME`, `-gf`, `--git-token-file URL=DATEI`, `-I`, `--git-identity` | Aus der Sandbox per HTTP(S) pushen, auch nach github.com |
| `-p`, `--profile development` | Anbieter, PyPI, GitHub und die APIs von OpenAI und Anthropic |
| `-S`, `--state-dir DIR` | Ein eigenes Sandbox-HOME, zwischen Läufen behalten |
| `-o`, `--offline`, `-E`, `--egress-all-allow` | Gar kein Netz / jedes ausgehende Ziel |
| `-gu`, `--gui`, `-gv`, `--gui-vnc DIR` | Virtuelles Display für GUI-Programme und Browser |
| `-l`, `--log-dir DIR` | Blockierte Verbindungen und Dateizugriffe protokollieren |
| `-n`, `--dry-run`, `-pb`, `--probe` | Plan zeigen / Isolation ohne Agent prüfen |

### Backend und Image

| Option | Bedeutung |
|---|---|
| `-b`, `--backend podman\|bwrap` | Sandbox-Backend (Standard: `podman`) |
| `-i`, `--image IMAGE` | Fertiges Podman-Image (Standard: `localhost/<agent>-sandbox:latest`); wird nie implizit gezogen |
| `-iv`, `--image-variant NAME` | Ihre Image-Variante NAME dieses Agenten (`sandbox-ai images new NAME --from AGENT`): `localhost/<agent>-sandbox-NAME:latest`; nur Podman, nicht mit `--image` |
| `-pu`, `--podman-url URL` | URL des Podman-Dienstes, z. B. `unix:///example/podman.sock` |
| `-B`, `--binary PFAD` | Agent-Programm (Host-Pfad bei bwrap, Image-Pfad bei Podman) |
| `-rt`, `--runtime DIR` | bwrap: Laufzeitverzeichnis schreibgeschützt am selben Pfad einblenden; mehrfach |

Siehe [Agent starten](#agent-starten) und [Voraussetzungen](#voraussetzungen).

### Workspaces und Mounts

| Option | Bedeutung |
|---|---|
| `-w`, `--workspace DIR[,DIR...]` | Erstes Verzeichnis unter `/work`, weitere unter `/workspaces/<name>`; mehrfach |
| `-W`, `--workspace-ro` | Alle Workspaces schreibgeschützt einbinden |
| `-m`, `--mount SRC:DEST:ro\|rw` | Zusätzlicher Mount; `DEST` unter `/work`, `/mnt` oder `/opt`; mehrfach |

Siehe [Workspaces und Mounts](#workspaces-und-mounts).

### Anmeldung, Zustand und Umgebung

| Option | Bedeutung |
|---|---|
| `-ca`, `--copy-auth` | Kopie der Anmeldedateien dieses Agenten verlangen (Login-Speicher, sonst Host); ohne die Option: kopiert, wenn vorhanden und das Sandbox-HOME noch kein Login hat |
| `-na`, `--no-auth` | Keine Anmeldedaten des Hosts kopieren |
| `-af`, `--auth-file DATEI` | Diese JSON-Datei an den Standard-Anmeldepfad des Agenten kopieren |
| `-ic`, `--import-codex-auth DATEI` | Nur OpenCode: eine Codex-ChatGPT-`auth.json` als private OAuth-Kopie importieren |
| `-cp`, `--copy SRC:ZIEL_RELATIV_ZU_HOME` | Einzelne Konfigurations- oder Anmeldedatei ins Sandbox-HOME kopieren; mehrfach |
| `-S`, `--state-dir DIR` | Privates, dauerhaftes Sandbox-HOME (Modus 0700), Ihr eigenes; ohne die Option bewahrt der Start sein HOME im eigenen Verzeichnis auf (`--no-keep`: nach dem Ende gelöscht) |
| `-e`, `--env NAME` | Eine Host-Variable nur an den Agenten geben; Werte werden nie ausgegeben; mehrfach |
| `-se`, `--secret-env NAME[=GEHEIMNIS]` | Der Agent bekommt NAME mit dem Wert eines gespeicherten Geheimnisses (Standard: das Geheimnis NAME); mehrfach ([Geheimnisse](#geheimnisse)) |
| `-id`, `--identity NAME` | Als [Identität](#identitäten-sandbox-ai-identities---identity) arbeiten: ihr Commit-Name und ihre Adresse, ihre Git- und Publish-Logins statt der von default |
| `-pl`, `--publish ART[,ART]` | Mit Ihren [Publish-Logins](#veröffentlichen-in-registries-und-forges-sandbox-ai-publish-logins---publish) veröffentlichen (npm, pypi, testpypi, crates, gitlab, github): öffnet ihre Hosts, Platzhalter für die Werkzeuge, Tokens fügt der Proxy ein, jede Veröffentlichung wird erfragt; mehrfach |
| `-mm`, `--model MODELL` | Das Modell des Agenten: Alias von Claude Code (`fable`, `opus`, `sonnet`, `haiku`) oder voller Name, Modell von Codex, `anbieter/modell` von OpenCode; mit `--llm` ist das Modell `--llm NAME:MODELL` |
| `-ef`, `--effort STUFE` | Wie gründlich der Agent denkt: Claude Code `low`, `medium`, `high`, `xhigh`, `max` (`--effort`); Codex `minimal` … `xhigh` (`model_reasoning_effort`); OpenCode stellt es pro Modell in seiner Konfiguration ein |
| `-ky`, `--api-key HOST=NAME[=GEHEIMNIS]` | Der Agent bekommt NAME mit einem Platzhalter; der Proxy setzt das gespeicherte Geheimnis erst auf der Leitung ein, nur in einen Anmelde-Header von HTTPS an HOST; öffnet HOST; mehrfach ([Geheimnisse](#geheimnisse)) |

Siehe [Anmeldung und Zustand](#anmeldung-und-zustand).

### Kits, Projekt-Memory und Plugins

| Option | Bedeutung |
|---|---|
| `-k`, `--kit NAME` | Zusätzlich das Kit `~/.local/share/sandbox-ai/kits/NAME/AGENT`, sonst das eingebaute Kit NAME von sandbox-ai (`egm`, nur Claude); wiederholbar |
| `-K`, `--kit-dir DIR` | Zusätzlich das Kit in `DIR/AGENT`; wiederholbar |
| `-nk`, `--no-default-kit` | Ohne das Standard-Kit `~/.local/share/sandbox-ai/home/AGENT` |
| `-nm`, `--no-memory` | Kein Projekt-Memory (Memory-Datei und Claudes Auto-Memory werden weder geladen noch behalten) |
| `-np`, `--no-plugins` | Keine Plugins aus dem Plugin-Store (Claude) bzw. den Kits (OpenCode); ein wiederverwendetes Claude-HOME verliert die Plugin-Einstellungen früherer Läufe |

Siehe [Kits, Projekt-Memory und Plugins](#kits-projekt-memory-und-plugins).

### Netzwerk

| Option | Bedeutung |
|---|---|
| `-p`, `--profile NAME` | `development`, `frontier`, `dns`, `egress-all` oder `offline`; mehrfach |
| `-f`, `--frontier` | Zusätzlich API- und Anmelde-Hosts von OpenAI und Anthropic (TCP/443) |
| `-nd`, `--no-provider-defaults` | Standard-Hosts des Agenten weglassen |
| `-y`, `--allow-pypi`, `-g`, `--allow-github`, `-gl`, `--allow-gitlab`, `-ak`, `--allow-kernel-org`, `-dd`, `--allow-duckduckgo`, `-an`, `--allow-npm` | Host-Vorlagen (TCP/443) |
| `-A`, `--allow-host HOST` | Hostnamen pinnen und TCP/443 erlauben; mehrfach |
| `-a`, `--allow IP_ODER_CIDR` | Alle Protokolle und Ports zu diesem Ziel; mehrfach |
| `-t`, `--allow-tcp IP:PORT` | Genau ein TCP-Port auf einer Adresse (IPv6: `[ADDR]:PORT`); mehrfach |
| `-P`, `--allow-host-port PORT` | Podman: `127.0.0.1:PORT` in der Sandbox erreicht denselben Port am Host-Loopback; mehrfach |
| `-R`, `--dns` | Echtes DNS über `1.1.1.1` und `8.8.8.8` (UDP/TCP 53 nur zu diesen) |
| `-ns`, `--nameserver IP` | Stattdessen diesen Resolver nutzen; impliziert `--dns`; mehrfach |
| `-E`, `--egress-all-allow` | Jedes ausgehende Ziel erlauben; impliziert `--dns`; eingehend bleibt zu |
| `-o`, `--offline` | Kein Netz nach außen, auch nicht zum Anbieter |
| `-sn`, `--subnet CIDR` | Das eigene IPv4-Netz der Sandbox (pasta, slirp4netns): ein privates `/8` bis `/25`; die Sandbox `.100`, Gateway `.2`, DNS-Weiterleitung `.3` |
| `-s6`, `--subnet6 CIDR` | Podman: das eigene IPv6-`/64` der Sandbox in `fc00::/7`; die Sandbox `::100`, Gateway `::2` |
| `-nw`, `--network NAME`, `-ip`, `--ip IP`, `-i6`, `--ip6 IP` | Podman: vorhandenes privates Netz und feste Adressen |
| `-vp`, `--vpn PROFIL` | Podman: das VPN-Profil vorschalten, mit Kill Switch und Prüfungen (siehe [VPN](#vpn)) |
| `-vc`, `--vpn-check stop\|warn` | Was eine fehlgeschlagene VPN-Prüfung bewirkt (Standard: `stop`) |
| `-X`, `--no-proxy` | Kein Verbindungs-Proxy: kein Verbindungslog, Pushes prüfen nur die Hooks |
| `-x`, `--no-inspect HOST` | TLS zu HOST (oder `*.DOMAIN`) ungeöffnet durchreichen; mehrfach |
| `-ah`, `--allow-http 'HOST METHODEN PFAD'` | Nur passende Anfragen erreichen HOST (oder `*.DOMAIN`); mehrfach |
| `-ru`, `--rules DATEI` | Ein [Regelwerk](#regelwerke): erlauben, erfragen oder verbieten je HTTP-Anfrage und je Aufruf eines MCP-Werkzeugs; außerhalb aller Workspaces; mehrfach |
| `-Q`, `--ask on\|off` | Nachfragen bei allem, was die Optionen nicht erlauben; ohne Antwort nach 60 s abgewiesen (Standard: `on`; [Rückfragen](#rückfragen-und-browser-anfragen)) |
| `-cn`, `--canaries KATEGORIEN` | [Köderdateien](#köderdateien-canaries): `credentials` und `home` (Standard), `workspace`, durch Kommas getrennt, oder `all` |
| `-nn`, `--no-canaries` | Keine Köderdateien |
| `-tm`, `--telemetry STUFE` | [Telemetrie der Agenten](#telemetrie-der-agenten): `off` (Standard), `strict`, `on` |

Siehe [Netzwerkregeln](#netzwerkregeln) und [Verbindungs-Proxy](#verbindungs-proxy).

### Git

| Option | Bedeutung |
|---|---|
| `-ge`, `--git-token-env URL=NAME` | Token aus der Host-Variable `NAME` für Git unter `URL` (`http(s)://[USER@]HOST[:PORT]`); Loopback-URLs implizieren `--allow-host-port`, andere `--allow-host`; mehrfach |
| `-gf`, `--git-token-file URL=DATEI` | Wie `--git-token-env`, Token aus einer privaten Datei (Modus `0600`, außerhalb der Workspaces); mehrfach |
| `-tp`, `--token-proxy` | Git-Tokens bleiben beim Verbindungs-Proxy, der sie in Git-Anfragen an ihren HTTPS-Server einsetzt; der Agent liest sie nie |
| `-gc`, `--git-credentials DATEI` | Alle Logins aus `DATEI` (Format von Gits Credential-Store, z. B. `~/.git-credentials`; privat, außerhalb der Workspaces), jeweils mit den Netzwerkregeln von `--git-token-env` |
| `-I`, `--git-identity` | Als globaler `user.name` und `user.email` des Hosts committen |
| `-pc`, `--privacy-check public\|always\|off` | Nach dem Lauf zeigen, was der nächste Push veröffentlichen würde und privat bleiben sollte: bei Workspaces mit öffentlichem Remote (Standard), bei jedem Git-Workspace oder nie; außer bei `off` warnt der `pre-commit`-Hook des Agenten und `pre-push` zu einem öffentlichen Remote verweigert |
| `-pr`, `--privacy-rules VERZ` | Eigene Regeln: `VERZ/global.rules.toml` und `VERZ/PROJEKT.rules.toml` (oder `.json`); außerhalb der Workspaces, nie für den Agenten sichtbar; Standard `~/.config/sandbox-ai/privacy`, falls vorhanden |

Siehe [Netzwerkregeln](#netzwerkregeln) (Pushen nach GitHub und auf einen eigenen Git-Server) und
[Datenschutzprüfung vor dem Veröffentlichen](#datenschutzprüfung-vor-dem-veröffentlichen).

### Agentenverhalten, MCP und LLM-Server

| Option | Bedeutung |
|---|---|
| `-D`, `--dangerously-skip-permissions` | Agent ohne Rückfragen starten |
| `-lm`, `--llm NAME[:MODELL]` | Den LLM-Server NAME aus `sandbox-ai llm` nutzen: öffnet genau seine Adresse und seinen Port, konfiguriert den Agenten, der Schlüssel kommt vom Proxy (der Agent sieht einen Platzhalter); keine Cloud-Anmeldung kopiert; ein mit `--vpn PROFIL` angelegter Server schließt `--vpn PROFIL` ein |
| `-L`, `--local-config DATEI` | OpenCode: rein lokale Konfiguration, Cloud-Zugriff abgeschaltet |
| `-M`, `--mcp NAME` | Lokalen MCP-Server in eigener Sandbox anbinden; wird bei Bedarf gestartet; mehrfach |

Siehe [Agenten ohne Rückfragen](#agenten-ohne-rückfragen),
[LLM-Server](#llm-server-lokal-und-entfernt) und [Lokale MCP-Server](#lokale-mcp-server).

### Sessions, tmux und GUI

| Option | Bedeutung |
|---|---|
| `-N`, `--name NAME` | Name dieses Starts (Vorgabe `AGENT-PROJEKT-xxxx`); `sandbox-ai list` zeigt ihn, die übrigen Befehle nehmen ihn; abgelehnt, solange ein anderer Start unter ihm läuft oder aufbewahrt ist |
| `-pn`, `--project-name NAME` | Der PROJEKT-Teil des automatischen Namens (Vorgabe: der Ordnername des Haupt-Workspace; Schlüssel `"name"` der Projektdatei) |
| `-kp`, `--keep`, `-nkp`, `--no-keep` | HOME, Optionen und Logs beim Ende aufbewahren, damit `sandbox-ai resume NAME` den Start zurückholt (Standard); `--no-keep` verwirft das HOME (passt zu Skripten; Schlüssel `"keep"` der Projektdatei) |
| `-s`, `--session NAME` | Eine aufbewahrte, benannte Session (die ältere Form von `--name`): eine gestoppte dieses Namens kommt zurück |
| `-T`, `--tmux`, `-nt`, `--no-tmux` | tmux in der Sandbox (Fenster `agent` und `shell`) ist im Terminal Standard; `--tmux` besteht darauf, `--no-tmux` schaltet es ab |
| `-gu`, `--gui` | Virtuelles X-Display `:99` (Xvfb) |
| `-gs`, `--gui-screen BxHxTIEFE` | Größe des virtuellen Bildschirms (Standard: `1920x1080x24`) |
| `-gv`, `--gui-vnc DIR` | Impliziert `--gui`; VNC über den Unix-Socket `DIR/vnc.sock` |
| `-gw`, `--gui-wayland` | Wayland-Socket des Hosts freigeben (schwächt die Isolation) |

Siehe [Jeder Start](#jeder-start-namen-lösen-aufbewahren-fortsetzen), [tmux](#tmux-modus)
und [GUI](#gui-programme-und-browser).

### Hardware

| Option | Bedeutung |
|---|---|
| `-d`, `--device PFAD\|usb:VID:PID[:SERIENNUMMER]` | Ein Gerät des Hosts an den Agenten geben: zeichenorientiertes Gerät unter `/dev`, `/dev/bus/usb`, `/dev/dri`, `/dev/snd` als Ganzes, oder ein USB-Gerät über seine IDs (nach dem Neuanstecken wieder eingehängt); mehrfach (schwächt die Isolation) |
| `-G`, `--gpu nvidia\|dri\|rocm` | Die GPU-Geräte (NVIDIA mit Podman über CDI) |
| `-me`, `--memory GRÖSSE`, `-cu`, `--cpus N`, `-pi`, `--pids N` | Ressourcenlimits (Standard `8g`, die Hälfte der CPUs, `4096`; `none` für keines; [Limits](#ressourcenlimits)) |

Siehe [Hardware](#hardware-geräte-und-gpus); ein Dienst auf dem Host, erreicht
über `--allow-host-port`, ist sicherer.

### Konfiguration, Logs und Prüfungen

| Option | Bedeutung |
|---|---|
| `-c`, `--config DATEI` | Projektkonfiguration (Standard: vertrauenswürdige `.sandbox-ai.json` im Haupt-Workspace) |
| `-nc`, `--no-config` | `.sandbox-ai.json` ignorieren |
| `-l`, `--log-dir DIR` | `egress-denied.jsonl`, `files.jsonl`, `connections-*.jsonl` und `activity.jsonl` dorthin schreiben (für den Agenten unsichtbar); Verbindungslog und Aktivität sonst in `~/.local/state/sandbox-ai/logs/PROJEKT-HASH` |
| `-ld`, `--log-debug` | Mehr über Abgewiesenes festhalten: Bodies, das TLS-ClientHello, alle Vorfahren und Sockets des Prozesses; Zugangsdaten bleiben geschwärzt |
| `-n`, `--dry-run` | Plan ausgeben; kein DNS, kein Lesen von Anmeldedaten, keine Kopien, Pods oder Prozesse |
| `-pb`, `--probe` | Beide Sandbox-Schichten und die Firewall prüfen, dann ohne Agent beenden |
| `-h`, `--help` | Vollständige Hilfe |
| `-V`, `--version` | Version (jeder Befehl hat sie) |

Siehe [Projektkonfiguration](#projektkonfiguration-und-wizard) und
[Logs](#egress--und-dateizugriffs-logs).

### Hilfsbefehle

| Befehl | Argumente |
|---|---|
| `sandbox-ai BEFEHL …` | ein Befehl für alles Folgende, dazu `doctor`, `logs [VERZ\|--project P] [--json]`, `update [--check]`, `version` |
| `sandbox-ai activity [LAUF]` | `[-s NAME\|-p PFAD\|-l VERZ\|--home VERZ] [-f] [--since ZEIT] [-k ART[,ART]] [-g TEXT] [-n N] [-v] [-j] [--runs]`: die Zeitleiste der Agenten (siehe [Aktivität der Agenten](#aktivität-der-agenten)) |
| `sandbox-ai privacy-check [PFAD ...]` | `[--rules-dir VERZ] [--project NAME] [--all] [--json]`: was der nächste Push jedes Repositorys veröffentlichen würde und privat bleiben sollte; Exit 1 bei blockierenden Funden, 2 wenn nicht prüfbar |
| `sandbox-ai net list\|show\|allow\|revoke [LAUF] [--allow-host H] [--allow-tcp IP:PORT] [--allow NETZ] [--allow-PRESET] [--dns] [--nameserver IP] [--allow-host-port PORT] [--git-token-env\|--git-token-file\|--git-token-secret URL=REF]`, `allow ... --keep` | Ändern, was eine laufende Sandbox erreichen darf; `--keep` schreibt es auch in die `.sandbox-ai.json` des Projekts (siehe [Netzwerkregeln](#netzwerk-einer-laufenden-sandbox-ändern)) |
| `sandbox-ai net stop LAUF [--wait SEKUNDEN]` | Die ältere Form von `sandbox-ai stop NAME` für einen Lauf: SIGTERM an ihren geprüften Launcher, der aufräumt |
| `sandbox-ai net untaint [LAUF]` | Den Taint des Laufs zurücknehmen: Die Regeln für Läufe mit Taint gelten nicht mehr, bis wieder nicht vertrauenswürdiger Inhalt hineinkommt (siehe [Regelwerke](#regelwerke)) |
| `sandbox-ai devices list\|add\|remove [LAUF] PFAD\|usb:VID:PID[:SERIAL]`, `sandbox-ai limits [LAUF] [--memory GRÖSSE] [--cpus N] [--pids N]` | Einer laufenden Sandbox Geräte geben und wieder nehmen; ihre Limits ändern |
| `sandbox-ai workspace list [LAUF] [--json]`, `add [LAUF] VERZ\|DATEI [--ro] [--name NAME]`, `remove [LAUF] VERZ\|DATEI\|NAME [--force]` | Ein Host-Verzeichnis (oder eine einzelne Datei) in eine laufende Sandbox unter `/workspaces/<name>` (oder `/workspaces/NAME`) einhängen (die Prüfungen von `--workspace`; ein Verzeichnis bleibt für den nächsten Start einer Session, unter seinem Namen) und wieder wegnehmen |
| `sandbox-ai approvals list\|history [LAUF]`, `allow ID [--scope once\|run\|always]`, `deny ID`, `remembered`, `forget PROJEKT EINTRAG` | Die Fragen laufender Sandboxen beantworten; was je Projekt immer erlaubt wurde, und einen Eintrag davon vergessen |
| `sandbox-ai canaries list [LAUF] [--tokens] [--json]`, `canaries log [LAUF] [--json]`, `canaries resume LAUF` | Die [Köderdateien](#köderdateien-canaries) der Läufe und was mit ihnen geschah; jedes Ereignis mit Prozess, Tool-Aufruf und Modellanfragen; eine von einem Canary pausierte Sandbox fortsetzen |
| `sandbox-ai secrets list\|set NAME [--from-env VAR]\|delete NAME\|backend` | Geheimnisse im System-Schlüsselbund, sonst in privaten Dateien |
| `sandbox-ai git-logins add URL --secret NAME [--user USER]`, `list [--json]`, `remove URL`, `check [URL]` | Die Logins Ihrer Git-Server, vom Verbindungs-Proxy eingesetzt (siehe [Git-Logins](#git-logins-im-proxy-sandbox-ai-git-logins)) |
| `sandbox-ai publish-logins add KIND --secret NAME [--url URL]`, `list [--json]`, `remove KIND`, `check [KIND]`, `kinds` | Logins für `--publish` (siehe [Veröffentlichen](#veröffentlichen-in-registries-und-forges-sandbox-ai-publish-logins---publish)) |
| `sandbox-ai identities add\|set NAME [--name NAME] [--email ADRESSE]`, `login NAME URL --secret SECRET`, `publish NAME KIND --secret SECRET`, `list\|show\|check\|remove …` | Identitäten für `--identity` (siehe [Identitäten](#identitäten-sandbox-ai-identities---identity)) |
| `sandbox-ai presets list\|show NAME\|check NAME [--value KEY=WERT]... [--json]` | Presets für `--preset` (siehe [Presets](#presets)) |
| `sandbox-ai demo-device start\|serve\|status\|stop` | Das nachgebildete serielle Gerät zum Üben (siehe [Presets](#presets)) |
| `sandbox-ai team start NAME --agents A,B[,C] [-w VERZ] [-- Optionen]`, `list\|show\|post\|stop\|delete …` | Ein Team von Agenten (siehe [Ein Team von Agenten](#ein-team-von-agenten)) |
| `sandbox-ai hosts add NAME USER@HOST [--port PORT] [--identity DATEI]`, `list\|remove\|test NAME`, `run NAME -- ARGS...` | Andere Rechner, über SSH verwaltet (siehe [Andere Hosts](#andere-hosts)) |
| `sandbox-ai web user\|vendor\|serve\|cert …`, `sandbox-ai defaults show\|set\|unset …` | Die Weboberfläche und Ihre Vorgaben (siehe [Weboberfläche](#weboberfläche)) |
| `sandbox-ai completion bash\|zsh\|fish\|install` | Skripte für die Tab-Vervollständigung, oder sie einrichten |
| `sandbox-ai vpn add NAME --wireguard DATEI\|--openvpn DATEI\|--mullvad\|--tailscale\|--tor [...]`, `vpn list\|show NAME\|remove NAME\|check NAME [--json]` | VPN-Profile für `--vpn` (siehe [VPN](#vpn)) |
| `sandbox-ai images [--json]`, `sandbox-ai images packages AGENT\|VARIANTE [--json]` | Agent-Images und Varianten, ob sie zu ihrer Erweiterung passen, und ihre Pakete (offline aus dem Image gelesen) |
| `sandbox-ai images extend\|check\|recipe\|new\|remove\|history …` | Erweiterungen, fertige Rezepte (`media`, `tor`) und Varianten (siehe [Erweiterungen und Varianten](#erweiterungen-und-varianten)) |
| `sandbox-ai build` | `[--podman-url URL] [--pin VERSION] [--uv VERSION] [--node IMAGE] [--extension-only] AGENT\|all\|VARIANTE [podman-build-Optionen]`; neueste Versionen, sofern nicht fixiert; danach die Erweiterungsschichten. `sandbox-ai build [--podman-url URL] team\|vpn` baut das Team-Image oder den VPN-Container |
| `sandbox-ai list [--all] [--json]`, `list --history NAME` | Jeder Start: was läuft oder pausiert, die aufbewahrten Starts, MCP-Server; der Verlauf eines Namens |
| `sandbox-ai attach\|pause\|resume\|stop NAME` | Verbinden (lösen mit `Ctrl-b d` oder durch Schließen des Terminals), einfrieren, auftauen bzw. einen gestoppten aufbewahrten Start zurückholen, stoppen |
| `sandbox-ai resume NAME --review-home [--json]` | Was sich in den Startdateien des aufbewahrten HOME seit dem letzten Start geändert hat; startet nichts |
| `sandbox-ai rename ALT NEU`, `sandbox-ai delete NAME [--yes]` | Ein anderer Name (die Run-ID bleibt); einen gestoppten Start samt HOME entfernen |
| `sandbox-ai gc [--older-than TAGE] [--dry-run] [--json]` | Reste entfernen, und aufbewahrte Starts, die TAGE Tage unbenutzt sind |
| `sandbox-ai session list\|attach\|pause\|resume\|stop\|delete` | Die ältere Form (`sandbox-session`, veraltet) |
| `sandbox-ai mcp list` | Definierte MCP-Server und ihr Status |
| `sandbox-ai mcp catalog`, `sandbox-ai mcp add NAME [-m ZIEL=HOST[:ro\|rw]] [-e VAR] [-n none\|full] [-M GRÖSSE] [-p N] [-v VPN]` | Server der MCP-Kataloge; einen als Container-Server hinzufügen (siehe [Lokale MCP-Server](#server-aus-einem-mcp-katalog)) |
| `sandbox-ai mcp start NAME [--backend podman\|bwrap] [--vpn PROFIL] [--for START [--restart]]`, `sandbox-ai mcp stop NAME` | MCP-Server-Sandbox starten oder stoppen (`--for`: für diesen Start, mit ihm verbunden) |
| `sandbox-ai mcp attach NAME START [--restart \| --live] [--json]`, `--helper AGENT START --live` | Einen MCP-Server (oder einen Helfer) mit einem behaltenen Agenten-Start verbinden (ein laufender: damit neu gestartet, oder `--live`: sofort, ohne Neustart) |
| `sandbox-ai mcp detach NAME START [--live] [--json]`, `--helper AGENT START [--live]` | Ihn aus den Optionen des Starts nehmen (`--live`: und sein Relay in der laufenden Sandbox sofort schließen) |
| `sandbox-ai wizard [new]` | Interaktive Projekteinrichtung; bietet an, eine vorhandene Konfiguration zu bearbeiten |
| `sandbox-ai wizard edit [VERZ\|DATEI]` | Vorhandene Konfiguration eines Projekts ändern (Menü, Änderungen vor dem Schreiben) |
| `sandbox-ai login AGENT [Launcher-Optionen] [-- Login-Argumente]`, `--status`, `AGENT --logout` | Login-Speicher für Sandboxes |
| `sandbox-ai plugins [claude [--update] [--marketplaces-only] [Launcher-Optionen] [-- claude-plugin-Argumente]] [--status]` | Plugin-Store für Claude-Sandboxes |
| `sandbox-ai plugins marketplaces [MARKTPLATZ] [--agent claude\|codex\|opencode] [--json]` | Jeder Plugin-Marktplatz (der von sandbox-ai, der Kits, des Stores, von Claude, von Codex; die npm-Plugins von OpenCode) mit dem Zustand jedes Plugins; nur lesend |
| `sandbox-ai kits [list [--json] [--agent A]]`, `show NAME [--json]` | Alle Kits und was sie pro Agent mitbringen; ein Kit (siehe [Kits verwalten](#kits-verwalten-sandbox-ai-kits)) |
| `sandbox-ai kits new NAME [--agent A]...`, `delete NAME --yes` | Ein eigenes Kit anlegen oder löschen |
| `sandbox-ai kits add-skill NAME PFAD [--agent A]... [--as SKILL]`, `new-skill NAME SKILL [--agent A]... [--description TEXT]`, `remove-skill NAME SKILL [--agent A]...` | Skill-Ordner ins Kit kopieren (Schnappschuss, kein Symlink wird verfolgt), einen aus einer Vorlage schreiben, einen entfernen |
| `sandbox-ai kits add-plugin NAME PLUGIN [--agent claude\|opencode] [--source QUELLE]`, `remove-plugin NAME PLUGIN [--agent A]`, `add-marketplace NAME MARKETPLACE QUELLE` | Plugins und Marketplaces in der `marketplaces.json` eines Kits deklarieren (QUELLE: `OWNER/REPO`, `https://...`, `sandbox-ai`; NAME: ein eigenes Kit oder `default`) |
| `sandbox-ai kits enable-plugin NAME PLUGIN [--agent A]`, `disable-plugin NAME PLUGIN [--agent A]` | Ein Plugin eines Kits ein- oder ausschalten (aus: deklariert und installiert, nicht aktiviert; auch über das eines früheren Kits) |
| `sandbox-ai kits remove-marketplace NAME MARKTPLATZ [--with-plugins]`, `import-marketplace NAME MARKTPLATZ [--from claude\|codex] [--with-plugins]` | Einen Marktplatz aus einem Kit nehmen; einen, den Claude oder Codex auf diesem Rechner schon kennt, mit seiner dortigen Quelle deklarieren |
| `sandbox-ai kits add-hook NAME DATEI [--agent A]... [--event E] [--matcher M] [--timeout S] [--as DATEI]`, `remove-hook NAME DATEI [--agent A]...` | Einen Hook ins Kit aufnehmen (ein Skript, eine `hooks.json`, ein OpenCode-Plugin) oder entfernen ([Hooks in Kits](#hooks-in-kits)) |
| `sandbox-ai kits trust-hooks KIT [--agent A]... [--yes\|--revoke]` | Die Hooks eines Kits prüfen (jeder Hook und jede Datei wird gezeigt) und genau diesen Inhalt freigeben, oder die Freigabe zurücknehmen |
| `sandbox-ai wizard trust DATEI [--yes]`, `sandbox-ai wizard forget DATEI` | Projektkonfiguration vertrauen bzw. von der Vertrauensliste entfernen |
| `sandbox-ai llm add NAME --kind ART --url URL [...]`, `list`, `show NAME`, `remove NAME`, `models NAME`, `check NAME [--agent AGENT]` | LLM-Server, lokal oder entfernt, für `--llm` (siehe [LLM-Server](#llm-server-lokal-und-entfernt)) |
| `sandbox-ai wizard models --kind ollama\|lmstudio\|llamacpp\|vllm --host HOST [--port PORT] [--sort name\|size\|loaded]`, `sandbox-ai wizard models --llm NAME` | Modelle eines lokalen LLM-Servers auflisten, oder eines angelegten |
| `sandbox-ai wizard models --kind ART --url URL [--ca DATEI] [--api-key-secret NAME] [--insecure-http]` | Die Modelle eines noch nicht angelegten Servers (Verbindung testen in App und Web); ein Schlüssel geht über reines http nur an lokale Adressen, sonst nur mit `--insecure-http`, wie bei `llm add` |

## Tests

```bash
# Unit-, Contract-, Matrix- und End-to-End-Tests (startet keine Sandbox)
python3 -m pytest tests/test_agent_profiles.py tests/test_agent_sandbox.py \
  tests/test_agent_sandbox_{contracts,matrix,edges}.py tests/test_sessions.py tests/test_starts.py \
  tests/test_home_review.py tests/test_feature_matrix.py tests/test_wizard.py tests/test_modes.py \
  tests/test_mcp.py -q

# Live-Tests: echte Podman-/Bubblewrap-Sandboxes
python3 -m pytest tests/test_agent_sandbox_live.py --agent-live -q
# … plus echte Modellaufrufe, Internet-Downloads, ein lokaler LLM-Server
python3 -m pytest tests/test_agent_sandbox_live.py --agent-live --agent-provider-tests \
  --agent-network-tests --local-llm ollama:192.168.1.20:11434 -q
```

Jede Starterfunktion hat happy-, sad- und edge-Contracts; ein Manifest-Test
lehnt neue Funktionen ohne sie ab. Matrizen kombinieren Agenten, Backends,
Auth-Modi und Workspace-Zustände; der End-to-End-Test des Wizards steuert das
echte Skript `sandbox-ai wizard` gegen einen lokalen Ollama-kompatiblen
HTTP-Server und prüft den daraus folgenden Starter-Plan; der MCP-Test schickt
echtes JSON-RPC mit zwei gleichzeitigen Clients durch Socket-Server und Bridge
zum Demo-Server. Live-Tests decken
Isolation, Auth-Kopien und Mounts, die Firewall in jedem Modus, mehrere
Workspaces, Logs, GUI/VNC, Wayland (falls vorhanden), tmux, MCP-Server in
eigener Sandbox, den gesamten
Session-Lebenszyklus (Pause friert wirklich ein, Resume läuft weiter, Stop
behält den Zustand), Image-Werkzeuge und optional praktische Modellläufe,
Playwright und ein lokales LLM ab. Optionen: `--agent-backend podman|bwrap`,
`--agent-image`, `--agent-podman-url`, `--codex-model`, `--claude-model`,
`--free-model`, `--opencode-model`. Praktische Modellläufe schreiben ihre
Berichte nach `evidence/` (nicht versioniert, damit Berichte immer zum Code
passen, der sie erzeugt hat).

`tests/test_hosts_ssh.py` testet `sandbox-ai hosts` durchgehend: der echte
OpenSSH-Client gegen einen SSH-Server auf 127.0.0.1 (der von asyncssh, also
ohne sshd: `pip install asyncssh`; ohne ihn übersprungen). Geprüft werden
Anmeldung per Schlüssel mit BatchMode, Argumente mit Anführungszeichen,
`$(...)` und Globs durch eine echte entfernte Shell, Exit-Status, stderr,
Standardeingabe und große Ausgaben, ein Terminal für attach (`ssh -tt`), der
einmal gelernte Host-Schlüssel und ein geänderter, der abgelehnt wird, ein
unbekannter Schlüssel oder Benutzer, sofort abgelehnt, niemand am Port, ein
eigener Befehl des Hosts und die Befehlszeile `sandbox-ai hosts`.

`tests/test_live_e2e.py` (ebenfalls `--agent-live`) prüft die Funktionen ab
0.1.20 auf einem echten Host: den Proxy hinter der echten NAT-Umleitung mit
echtem TLS, abgewiesene und angenommene Pushes zu einem Git-Server auf dem
Host, abgewiesenes SSH, Netzwerkänderungen und Rückfragen (Verbindung,
Namensauflösung, Browser-Anfrage) während eine Sandbox läuft, Geräte,
MCP-Katalog-Container, Ressourcenlimits in der cgroup des Containers,
Geheimnisse als Variablen und das Prozess-Log. Der GitHub-Actions-Workflow
`e2e` führt sie samt Image-Bau auf Ubuntu 24.04 mit rootless Podman und mit
Bubblewrap aus; Fehlschläge erscheinen als Annotationen des Jobs.

## Fehlersuche

Zuerst `sandbox-ai doctor` ausführen: Es prüft alle Voraussetzungen und nennt
zu jedem Problem den Befehl, der es behebt.

| Symptom | Prüfen |
|---|---|
| Claude „not logged in“ / „Login expired · Please run /login“ | `sandbox-ai login --status` zeigt, welches Login die Sandboxes bekommen. `sandbox-ai login claude` gibt ihnen ein eigenes, unabhängig vom Host (empfohlen, wenn Claude auch auf dem Host läuft: Erneuern beide dasselbe Login, ist einer abgemeldet). Der Refresh braucht `console.anthropic.com`/`platform.claude.com`, standardmäßig erlaubt. |
| Login-Code oder OAuth-Token abgelehnt, obwohl richtig kopiert | In tmux übernimmt die Oberfläche des Agenten die Maus; eine normale Auswahl kopiert nichts oder nur ein umbrochenes Stück: beim Markieren Shift gedrückt halten (in manchen macOS-Terminals Option/Alt) und den Code als eine Zeile einfügen. `sandbox-ai login claude` meldet ohne tmux an. |
| `Project configuration … is not trusted` | Datei prüfen, dann `./sandbox-ai wizard trust DATEI`, oder mit `--no-config` starten. |
| LLM-Server nicht erreichbar oder abgewiesen | `sandbox-ai llm check NAME --agent AGENT` (Adresse, TLS, Schlüssel, Modell, API); ohne `sandbox-ai llm`: Server ans LAN gebunden, LAN-IP des Hosts verwendet, `--allow-tcp IP:PORT` passt. |
| `Unknown MCP server` / Server startet nicht | `./sandbox-ai mcp list`; `~/.local/state/sandbox-ai/mcp/NAME/supervisor.log` lesen. |
| `-T`, `--tmux needs an interactive terminal` | `--tmux` weglassen (im Terminal ist tmux ohnehin Standard) oder für Hintergrundläufe `--session` verwenden. |
| `The trusted project configuration … is missing` | Datei wiederherstellen, mit `--no-config` starten oder `./sandbox-ai wizard forget DATEI`. |
| `… is a symlink; refusing` | Projektdateien und Sandbox-Anmeldedaten müssen reguläre Dateien sein; den Link ersetzen. |
| `inotify watch limit reached` | `fs.inotify.max_user_watches` erhöhen oder weniger Verzeichnisse beobachten. |
| Ein Build scheitert mit `sd-bus call: Permission denied` | Keine systemd-Benutzersitzung (ein LXC-Container, eine Anmeldung ohne systemd): Seit 0.1.30 nimmt `sandbox-ai build` dann von selbst `--cgroup-manager=cgroupfs`. Für Limits und den systemd-cgroup-Manager: `sudo loginctl enable-linger $USER` und neu anmelden (ein Container braucht systemd als Init); `sandbox-ai doctor` zeigt die Sitzung. |
| `pasta failed with exit code 1: Failed to open() /dev/net/tun` (Status 127) | pasta braucht `/dev/net/tun`; ein LXC- oder Proxmox-Container hat es nur, wenn sein Host es durchreicht: `lxc.cgroup2.devices.allow: c 10:200 rwm` und `lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file` in `/etc/pve/lxc/ID.conf`, dann den Container neu starten. Auf einem Host: `sudo modprobe tun`. `sandbox-ai doctor` prüft es zusammen mit pasta. |
| Codex meldet UID-Mapping-Fehler | Ein anderer expliziter Codex-Sandbox-Modus aktiviert dessen eigene Sandbox wieder. |
| In einem Proxmox- oder LXC-Container, gleich was | [PROXMOX_LXC_DE.md](PROXMOX_LXC_DE.md): was der Host und eine Container-Vorlage brauchen, und die Probleme einer ersten Einrichtung. |
| `Podman failed (exit status 125)`, `newuidmap: write to uid_map failed: Operation not permitted` (ein unprivilegierter LXC- oder Proxmox-Container) | Die delegierten IDs in `/etc/subuid` liegen außerhalb der IDs des Containers (meist nur 0-65535; `cat /proc/self/uid_map`). Auf dem Host: `root:100000:1065536` in `/etc/subuid` und `/etc/subgid`, `lxc.idmap: u 0 100000 1065536` und `lxc.idmap: g 0 100000 1065536` in `/etc/pve/lxc/ID.conf`, `features: nesting=1,keyctl=1,fuse=1`; Container neu starten, dann `podman system migrate`. Vorher geladene oder gebaute Images tragen noch die alten IDs: Ein Build scheitert dann mit `Permission denied` (apt auf `/var/cache/apt/archives/partial`), bis `podman system reset` (es entfernt Images und Container dieses Benutzers) und ein neues `sandbox-ai build` folgen. Scheitert schon das Zurücksetzen mit `unlinkat ... permission denied` (Dateien von IDs, die nicht mehr Ihre sind), entfernt root den Speicher: `sudo rm -rf ~/.local/share/containers/storage`, dann `podman system migrate`. Für den Proxy lädt der Host auch `nft_chain_nat` und `nft_redir`. `sandbox-ai doctor` prüft die Bereiche. Ein kleinerer Bereich, der in die IDs des Containers passt (z. B. `user:10000:50000`), ist kein Ausweg: Podman startet, aber jeder Image-Build scheitert in apt (`setgroups 65534 failed - Invalid argument`), das die ID 65534 braucht; nur mehr IDs für den Container helfen. |
| `newuidmap`/`newgidmap` schlägt fehl | Helfer, delegierte Bereiche, Hostpolicy prüfen. |
| Image fehlt | `./sandbox-ai build AGENT` oder `--image`. |
| Kopierziel existiert | Wiederholte Kopieroptionen bei wiederverwendetem `--state-dir` weglassen. |

## Grenzen und offene Arbeiten

1. **P1 – Zustandstrennung:** `/`, HOME, das temporäre Verzeichnis,
   `XDG_RUNTIME_DIR` und das Verzeichnis des Starters werden als Workspace
   abgelehnt, aber ein `--state-dir`, das zwei Läufe gleichzeitig nutzen, ist
   nicht gesperrt.
2. **P1 – macOS:** Die Sandboxes laufen in einer rootless `podman machine`.
   Für Starts im Vordergrund ist das im Code fertig, aber noch nicht auf einem
   echten Mac getestet. `--backend bwrap`, `--device`, `--gpu dri|rocm` und
   `--gui-wayland` werden dort abgelehnt. Ein Start läuft ohne tmux, außer
   `--tmux` verlangt es; das Verbinden mit einem Start, `sandbox-ai net
   allow|revoke` und `sandbox-ai limits` während eines Laufs, `--allow-host-port`
   und `--llm` mit einem Server auf dem Loopback des Macs selbst, `xdg-open` in
   der Sandbox, der Schlüsselbund (Keychain) und MCP-Server aus Images sind noch
   nicht portiert.
3. **P2 – Host-Abdeckung:** Der Workflow `e2e` führt die Live-Tests unter
   Ubuntu 24.04 mit beiden Backends aus; andere Distributionen und Kernel sowie
   Hardware (Geräte, GPUs, Wayland) sind nur durch Unit- und Contract-Tests
   abgedeckt.
4. **P2 – Betriebsgrenzen:** Ressourcenlimits brauchen eine
   systemd-Benutzersitzung; ohne sie starten Sandboxen unbegrenzt.
   `files.jsonl` und `egress-denied.jsonl` sind nicht größenbegrenzt (die
   Verbindungs-Logs und `processes.jsonl` schon).
5. **P2 – Claude-Lebenszyklus:** Der laufende Abgleich des Logins ist gegen
   einen nachgebildeten OAuth-Endpunkt und einen Client getestet, der wie
   Claude Code 2.1.317 erneuert, noch nicht über eine lange Sitzung gegen den
   echten Dienst; die Kontozugehörigkeit zurückgeschriebener Tokens wird nicht
   geprüft.
6. **P3 – Reproduzierbare Builds:** Builds nehmen bewusst die neuesten
   Versionen; `--pin`, `--uv` und `--node` fixieren Agent, uv und Basis-Image,
   apt-Pakete sind nicht fixiert.

Bestandene Funktionstests sind kein Kernel-Escape-Audit und keine Garantie für
jede Host-Konfiguration. Firejail läuft mit `--noprofile` und den eigenen
Optionen des Starters; persönliche Firejail-Profile werden nicht geladen.
