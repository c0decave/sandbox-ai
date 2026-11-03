# sandbox-ai

[English](README.md) · [Schnellstart-Tutorial](QUICKSTART_DE.md)

Linux-Starter, die **Codex**, **Claude Code** und **OpenCode** in zwei
Sandbox-Schichten ausführen, mit standardmäßig gesperrtem Netz:

```text
Host-Benutzer → rootless Podman-Pod oder Bubblewrap → Firejail → Agent
```

`sandbox-ai` (und die Kurzformen `codex-sandbox`, `claude-sandbox`,
`opencode-sandbox`) führen den Host-Starter `sandbox/agent_sandbox.py` aus; in
die Sandbox wird nur `sandbox/runtime.py` eingeblendet, schreibgeschützt, als
deren Bootstrap. Vor dem Agentstart prüft ein Guard, dass Firejail
zusätzliche Mount- und PID-Namespaces angelegt, alle fünf Capability-Mengen
geleert sowie NoNewPrivs und Seccomp aktiviert hat; jede fehlgeschlagene
Prüfung bricht ab. Ausgehender Verkehr ist per nftables auf den Provider des
Agenten beschränkt, solange Sie ihn nicht erweitern.

## Inhalt

1. [Voraussetzungen](#voraussetzungen)
2. [Installation und Updates](#installation-und-updates)
3. [Der Befehl sandbox-ai](#der-befehl-sandbox-ai)
4. [Images bauen und aktualisieren](#images-bauen-und-aktualisieren)
5. [Agent starten](#agent-starten)
6. [Workspaces und Mounts](#workspaces-und-mounts)
7. [Anmeldung und Zustand](#anmeldung-und-zustand)
8. [Kits, Projekt-Memory und Plugins](#kits-projekt-memory-und-plugins)
9. [Netzwerkregeln](#netzwerkregeln)
10. [Lokale LLMs mit OpenCode](#lokale-llms-mit-opencode)
11. [Lokale MCP-Server](#lokale-mcp-server)
12. [Projektkonfiguration und Wizard](#projektkonfiguration-und-wizard)
13. [Sessions: lösen, pausieren, fortsetzen, stoppen](#sessions-lösen-pausieren-fortsetzen-stoppen)
14. [tmux-Modus](#tmux-modus)
15. [GUI-Programme und Browser](#gui-programme-und-browser)
16. [Agenten ohne Rückfragen](#agenten-ohne-rückfragen)
17. [Egress- und Dateizugriffs-Logs](#egress--und-dateizugriffs-logs)
18. [Datenschutzprüfung vor dem Veröffentlichen](#datenschutzprüfung-vor-dem-veröffentlichen)
19. [Optionsübersicht](#optionsübersicht)
20. [Tests](#tests)
21. [Fehlersuche](#fehlersuche)
22. [Grenzen und offene Arbeiten](#grenzen-und-offene-arbeiten)

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
  installiert sein. Bubblewrap behält dein Terminal und startet daher nicht
  interaktiv auf Kerneln, die Eingabe-Injektion per `TIOCSTI` erlauben
  (`dev.tty.legacy_tiocsti` ungleich 0 oder Kernel vor 6.2). Eine
  NVM-Installation braucht eventuell ein explizites,
  schreibgeschütztes `--runtime`-Verzeichnis.

Kernel und Hostpolicy müssen die nötigen Namespaces, Helfer und nftables
zulassen; die Starter heben Hostbeschränkungen nicht auf.

## Installation und Updates

`install.sh` installiert die neueste Version oder aktualisiert darauf und hält
den Starter aus allen Workspaces heraus:

```bash
./install.sh                    # aus einem Checkout: neuestes Release der eingestellten Quelle
./install.sh --from .           # genau diesen Checkout
sandbox-ai-update               # später: auf das neueste Release aktualisieren
sandbox-ai-update --check       # installierte und neueste Version, ohne Änderung
./install.sh --uninstall        # Befehle und Installation entfernen (Sessions und Einstellungen bleiben)
```

| Modus | Dateien | Befehle |
|---|---|---|
| Benutzer (Standard) | `~/.local/share/sandbox-ai/versions/X.Y.Z`, Link `current` | Links in `~/.local/bin` und, falls vorhanden, `~/.bin` |
| System (`--system` oder als root) | `/usr/local/lib/sandbox-ai` (`--prefix`) | `/usr/local/bin` |

Releases kommen von `https://github.com/c0decave/sandbox-ai`, sofern
`--source URL` oder `SANDBOX_AI_SOURCE` kein anderes Gitea-, Forgejo- oder
GitHub-Repository nennt. Der Installer lädt `sandbox-ai-bundle-X.Y.Z.tar.gz`
und `SHA256SUMS`, bricht bei falscher Prüfsumme ab, entpackt nur reguläre
Dateien unterhalb eines Verzeichnisses und schaltet den Link `current` atomar
um; die vorige Version bleibt für ein Zurückrollen erhalten. Für ein privates
Repository einen Token in `SANDBOX_AI_TOKEN` legen: Er geht nur an den Host des
Repositorys, nie per unverschlüsseltem http außer an localhost. Vorhandene
Dateien in den bin-Verzeichnissen, die nicht zu sandbox-ai gehören, bleiben
unberührt (`--force` ersetzt sie).

`pip install sandbox-ai` (oder `pipx install sandbox-ai`) installiert dieselben
Befehle von PyPI, sobald Releases dort erscheinen. Ein Checkout funktioniert
auch ohne Installation (`./claude-sandbox`), darf aber selbst kein Workspace
sein: Der Agent könnte den Starter umschreiben. Wie Releases gebaut und
veröffentlicht werden, steht in [RELEASING.md](RELEASING.md).

## Der Befehl sandbox-ai

`sandbox-ai` bündelt alle Werkzeuge; die älteren Namen bleiben als Kurzformen.

| Befehl | Kurzform | Macht |
|---|---|---|
| `sandbox-ai claude\|codex\|opencode [Optionen] [-- Argumente]` | `claude-sandbox` … | einen Agenten starten |
| `sandbox-ai session …` | `sandbox-session` | lösbare Sessions |
| `sandbox-ai mcp …` | `sandbox-mcp` | MCP-Server-Sandboxen |
| `sandbox-ai wizard [new\|edit\|trust\|forget\|models] …` | `sandbox-wizard` | Projekt einrichten, bearbeiten und vertrauen |
| `sandbox-ai login claude\|codex\|opencode` | | in einer Sandbox anmelden; das Login gilt für alle Sandboxes dieses Agenten ([Login-Speicher](#login-speicher-für-sandboxes)) |
| `sandbox-ai login --status`, `sandbox-ai login AGENT --logout` | | welches Login die Sandboxes jedes Agenten bekommen; gespeichertes Login vergessen |
| `sandbox-ai plugins claude [--update]`, `sandbox-ai plugins --status`, `sandbox-ai plugins claude -- ARGS` | | die Claude-Plugins der Kits in den Plugin-Store installieren ([Plugins](#kits-projekt-memory-und-plugins)); deklariert vs. installiert zeigen; `claude plugin ARGS` auf dem Store ausführen |
| `sandbox-ai build [--pin V] AGENT\|all` | `sandbox-build-image` | Images bauen |
| `sandbox-ai doctor [--backend podman\|bwrap\|both] [--json]` | | Host prüfen: Benutzer, Python, User-Namespaces, Podman (rootless), pasta, subuid/subgid, Images mit Agent-Versionen, Bubblewrap-Werkzeuge, TIOCSTI, Host-Logins (nur Ablauf), Standard-Kit und Kits mit ihren Skills, Agents, Commands und Prompts, Claude-Plugins deklariert vs. installiert, das Memory des Projekts im aktuellen Verzeichnis, Git-Identität, Projekt-Vertrauen; zu jedem Problem der Befehl, der es behebt; Exit 1 bei einem Fehler |
| `sandbox-ai logs [VERZ\|--project P] [--json]` | | blockierte Verbindungen und Dateizugriffe eines `--log-dir` zusammenfassen und je blockiertem Ziel die passende Option vorschlagen (`--allow-pypi`, `--allow-host`, `--allow-tcp`, `--allow-host-port` …) |
| `sandbox-ai update [--check]` | `sandbox-ai-update` | eine mit `install.sh` gemachte Installation aktualisieren |
| `sandbox-ai version` | `--version` | Version |

## Images bauen und aktualisieren

```bash
./sandbox-build-image all                 # neueste Versionen, alle drei Images
./sandbox-build-image claude              # ein Image, neuestes Claude Code
./sandbox-build-image --pin 1.0.42 claude # genau diese Agent-Version
```

Jeder Agent hat ein eigenes `sandbox/Containerfile.<agent>` und das Image
`localhost/<agent>-sandbox:latest`; ein Image enthält nur die eigene CLI. Die
Starter bauen oder laden nie automatisch Images.

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
`host`/`dig`, `ping`, `traceroute`, `ss`/`ip` (iproute2), tmux, Firejail und
nftables; für `--gui` zusätzlich Xvfb, x11vnc, x11-utils, Schriften
und die Laufzeitbibliotheken der Playwright-Builds von Chromium und Firefox.
Keine Compiler-Toolchain; Projektwerkzeuge im Rezept ergänzen. `gh` kommt aus
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

## Agent starten

```bash
./claude-sandbox --workspace "$HOME/src/demo"
./codex-sandbox --workspace "$HOME/src/demo" \
  -- exec --skip-git-repo-check 'Lege hello.txt mit dem Inhalt Hallo an.'
./opencode-sandbox --workspace "$HOME/src/demo" --dry-run   # nur den Plan zeigen
./codex-sandbox --workspace "$HOME/src/demo" --offline --probe   # Isolation prüfen
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
In der Sandbox erneuerte Tokens werden nur zurückgeschrieben, wenn der Host
einen Login hatte und ihn während des Laufs nicht geändert hat; beim nächsten
Start ersetzt immer der Login des Hosts den der Sandbox.

**Codex** erhält standardmäßig `--sandbox danger-full-access`: Es verlässt sich
auf die geprüfte äußere Sandbox und Firejail, weil seine eigene verschachtelte
Sandbox darin kein UID-Mapping anlegen kann. Explizite Optionen `--sandbox`,
`-s` und `--dangerously-bypass-approvals-and-sandbox` bleiben erhalten.

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
per Symlink umlenken. `.git/hooks` und `.git/config` eines Workspace führen
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
./claude-sandbox --workspace "$HOME/src/app,$HOME/src/shared-lib" --workspace "$HOME/notes"
#   /work = app, /workspaces/shared-lib, /workspaces/notes
```

**Weitere Mounts:** `--mount QUELLE:ZIEL:ro|rw`, mehrfach möglich. Quellen
müssen existieren; Ziele müssen unter `/work`, `/mnt` oder `/opt` liegen.
Kommas, Socket- und Gerätedateien werden abgelehnt. Sockets in eingebundenen
Verzeichnissen können weiterhin Hostdienste erreichbar machen; nur Nötiges
einbinden.

## Anmeldung und Zustand

Ohne Auth-Option werden die vorhandenen Anmeldedateien des Agenten privat
(Modus `0600`) ins Sandbox-HOME kopiert, sobald dort noch kein Login liegt
(ein frisches HOME immer; ein wiederverwendetes `--state-dir` oder eine Session
behält ihr eigenes); fehlende Dateien verhindern den Start nicht. Quelle ist der
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
| `--copy-auth` | Kopie verlangen; fehlt die Hauptdatei, ist das ein Fehler. Ein Login in einem wiederverwendeten Sandbox-HOME bleibt erhalten. |
| `--no-auth` | Nichts kopieren. |
| `--auth-file DATEI` | Diese Datei an das Auth-Ziel des Agenten kopieren. |
| `--import-codex-auth DATEI` | Nur OpenCode: Codex-ChatGPT-OAuth in eine OpenCode-Anmeldung umwandeln. |
| `--copy QUELLE:ZIEL` | Beliebige Datei nach `ZIEL` relativ zum Sandbox-HOME kopieren. |
| `--state-dir VERZ` | Dauerhaftes Sandbox-HOME (Ihnen gehörend, Modus `0700`, außerhalb des Workspace). |

OS-Schlüsselbunde werden nicht exportiert: Ein Agent, der seine Anmeldung nur
im Schlüsselbund hält, braucht zuerst eine dateibasierte Anmeldung.

**Claude-Anmeldung.** Für Claude meldet der Start zusätzlich, wie lange der
kopierte Access-Token gültig ist. Anthropic rotiert OAuth-Refresh-Tokens; nur
die zuletzt erneuernde Seite hält ein gültiges Paar. Bei automatischer Kopie
oder `--copy-auth` wird `.credentials.json` daher nach `expiresAt` abgeglichen,
die neueste gewinnt: Neuere Daten der Quelle ersetzen vor dem Start die eines
wiederverwendeten Zustandsverzeichnisses, neuere Sandbox-Daten werden nach dem
Ende in die Quelle (Host oder Login-Speicher) zurückgeschrieben, aber nur, wenn
die Quelle beim Start ein Login hatte und dieses Login unverändert ist. Es zählt
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
Symlinks in einem wiederverwendeten Sandbox-HOME werden abgelehnt. Ein
fehlgeschlagenes Zurückschreiben wird gemeldet, ändert aber nie den Exit-Status
des Agenten. Restrisiko: Ein
kompromittierter Agent könnte Tokens eines anderen Kontos einschleusen, die der
Host dann nutzt. `--no-auth` oder `--auth-file` lassen die Hostdatei unberührt.
Hostdateien anderer Agenten werden nie geschrieben.

**Vor dem Start** prüft der Launcher das Login, das die Sandbox bekommt. Fehlt
es, sagt er das (`sandbox-ai login AGENT`); ist Claudes Refresh-Token
abgelaufen, fragt er im Terminal, ob jetzt in einer Sandbox angemeldet werden
soll; dieses Login landet im Login-Speicher für alle späteren Sandboxes.

### Login-Speicher für Sandboxes

Teilen sich Host und Sandboxes ein Login, teilen sie auch dessen rotierenden
Refresh-Token: Wer als Zweiter erneuert, ist abgemeldet. `sandbox-ai login
AGENT` gibt den Sandboxes ein eigenes Login:

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
spätere Sandboxes ersetzen; zwei gleichzeitig erneuernde Sandboxes konkurrieren
weiterhin um einen Refresh-Token.

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

Der erste Start eines Agents legt sein Standard-Kit aus den Vorlagen von
sandbox-ai an: mit einem Beispiel-Skill (`sandbox-example`, erklärt dem Agent
die Sandbox), einer Memory-Vorlage und für Claude dem offiziellen
Anthropic-Marketplace und dem Demo-Plugin von sandbox-ai. Ändere oder lösche
sie; ein vorhandenes Standard-Kit wird nie wieder verändert, ein gelöschtes
Beispiel bleibt gelöscht.

```bash
claude-sandbox                             # nur das Standard-Kit
claude-sandbox --kit review --kit python   # dazu zwei benannte Kits
codex-sandbox --kit-dir ~/meine-kits       # dazu ~/meine-kits/codex
claude-sandbox --no-default-kit --kit review
```

Die Einträge aller Kits werden zusammengeführt. Derselbe Skill-, Agent-,
Command- oder Prompt-Name aus zwei Kits oder zwei Memory-Vorlagen beenden den
Start mit einer Meldung, die beide Kits nennt; nichts wird still ersetzt.
sandbox-ai kopiert die Einträge in einen privaten Schnappschuss und hängt ihn
**nur lesbar** ein: Der Agent kann sie nutzen, nicht ändern. Symlinks in Kits
Ein Kit-Eintrag darf ein Symlink sein (praktisch für einen Skill aus einem
Git-Checkout); unterhalb eines Eintrags werden Links nur verfolgt, solange sie
in ihm bleiben. Weder ein Kit noch ein Link-Ziel darf in einem Workspace, einem
rw-Mount oder dem `--state-dir` liegen, und keines davon in einem Kit oder
Store; Schleifen, mehr als 5000 Dateien oder 64 MiB beenden den
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
persistentes HOME (`--state-dir`, Session) die Änderungen für den nächsten
Start; ein Wegwerf-HOME verliert sie. `--no-memory` startet ohne
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
sandbox-ai plugins --status           # deklariert vs. installiert, pro Kit
sandbox-ai plugins claude -- list     # beliebiger `claude plugin ...`-Befehl auf dem Store
```

Das Demo-Plugin `sandcastle@sandbox-ai` bringt `/sandcastle`: ein Roboter, der
in seiner Sandbox mit Förmchen und Eimer Sandburgen baut. Eine Claude-Sandbox,
die vor `sandbox-ai plugins claude` startet, nennt die fehlenden Plugins.
**OpenCode** bekommt die npm-Pakete aus `opencode/marketplaces.json`
(`{"plugins": ["name@1.2.3"]}`) als `plugin`-Einstellung; OpenCode installiert
sie selbst, dafür braucht es `--allow-host registry.npmjs.org`. Plugins für
**Codex** werden noch nicht unterstützt.

Kits, Memory und Plugins entfallen bei `--probe`, in MCP-Server-Sandboxes und in
der Login-Sandbox. `--copy` in einen Pfad, den sie füllen, wird abgelehnt, ein
Kit oder Store in einem Workspace ebenso. Restrisiko: Ein Plugin führt in jeder
Sandbox, die es aktiviert, Code aus (Hooks, MCP-Server); installiere nur
Plugins, denen du vertraust.

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
| `--profile NAME` | Ein benanntes Bündel (mehrfach): `development` = Anbieter + PyPI + GitHub + APIs von OpenAI und Anthropic; `frontier` = `--frontier`; `dns`, `egress-all`, `offline` = die Optionen unten. Der Wizard nutzt dieselben Profile. |
| `--frontier` | Zusätzlich alle OpenAI- und Anthropic-Ziele oben erlauben, für jeden Agenten. |
| `--allow-pypi` | `pypi.org`, `files.pythonhosted.org` |
| `--allow-github` | `github.com`, `api.github.com`, `codeload.github.com`, `raw.githubusercontent.com`, `objects.githubusercontent.com`, `release-assets.githubusercontent.com` |
| `--allow-gitlab` | `gitlab.com`, `registry.gitlab.com` |
| `--allow-kernel-org` | `kernel.org`, `www.kernel.org`, `git.kernel.org`, `cdn.kernel.org`, `mirrors.kernel.org`, `lore.kernel.org` |
| `--allow-duckduckgo` | `duckduckgo.com`, `html.duckduckgo.com`, `lite.duckduckgo.com`, `links.duckduckgo.com`, `api.duckduckgo.com` |
| `--allow-host NAME` | Namen auflösen und festschreiben; TCP/443 erlauben. |
| `--allow IP_ODER_CIDR` | Alle Protokolle und Ports zu diesem Ziel. |
| `--allow-tcp IP:PORT` | Genau einen TCP-Port auf einer Adresse (IPv6: `[ADR]:PORT`). |
| `--allow-host-port PORT` | Podman: `127.0.0.1:PORT` in der Sandbox erreicht denselben Port am Host-Loopback (mehrfach). |
| `--dns` | Echtes DNS über `1.1.1.1` und `8.8.8.8` (nur UDP/TCP 53 zu diesen). |
| `--nameserver IP` | Stattdessen diesen Resolver nutzen (mehrfach; impliziert `--dns`). |
| `--egress-all-allow` | Jedes ausgehende Ziel und jeden Port erlauben; impliziert `--dns`. |
| `--no-provider-defaults` | Standardziele des Agenten entfernen. |
| `--offline` | Überhaupt kein externes Netz. |
| `--network NAME`, `--ip`, `--ip6` | Podman: bestehendes privates Netz, statische Adressen. |

`--dns` löst Namen auf, öffnet aber nicht die aufgelösten Adressen; über
Anfragenamen können Daten hinausgelangen, daher ist das eine bewusste
Erweiterung. `--egress-all-allow` hält eingehenden Verkehr und Forwarding
geschlossen und das Host-Loopback verborgen, das lokale Netz ist aber
erreichbar, einschließlich Diensten, die der Host auf seinen LAN-Adressen
anbietet. `127.0.0.1` bezeichnet die Sandbox selbst, außer für Ports, die
mit `--allow-host-port` geöffnet sind. IP-Filterung kann Domains mit
gemeinsamen CDN-Adressen nicht unterscheiden. Podman nutzt `pasta`,
Bubblewrap `slirp4netns`; es werden keine Ports veröffentlicht.

**Dienste auf dem eigenen Host** (ein lokaler Git-Server, eine Datenbank) sind
mit `--allow-tcp` nicht erreichbar: `pasta` gibt der Sandbox die eigene
IP-Adresse des Hosts, diese Adresse führt also zurück in die Sandbox. Das
vermeidet Kollisionen mit anderen Rechnern im Netz, verbirgt aber den Host.
Stattdessen `--allow-host-port PORT` nutzen: pasta leitet dann
`127.0.0.1:PORT` in der Sandbox auf denselben Port am
Loopback des Hosts weiter, sonst wird nichts auf dem Host erreichbar. Ein
Git-Remote wie `http://localhost:3000/...` funktioniert in der Sandbox
unverändert. Host ist der Rechner, auf dem Podman läuft, mit `--podman-url`
also dieser. Unterstützt nur vom Podman-Backend mit seinem Standardnetz;
unvereinbar mit `--offline`, `--network` und `--backend bwrap`.

```bash
./claude-sandbox --workspace "$HOME/src/demo" --allow-host-port 3000
```

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
./claude-sandbox --git-token-env https://github.com=SANDBOX_GH_TOKEN --git-identity
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
genau zum Host des Remotes passen: Ein Token für `http://localhost:3000`
antwortet nicht für `http://127.0.0.1:3000`. Wie bei GitHub werden Helper
aus einer `~/.gitconfig` in der Sandbox für diese URL ignoriert, und der
Agent kann den Token lesen (als `SANDBOX_GIT_TOKEN_0`, ...); daher einen
auf die nötigen Repositories beschränkten Token anlegen.

```bash
export SANDBOX_GITEA_TOKEN=...   # Gitea: Einstellungen -> Anwendungen, Scope repository
./claude-sandbox --git-token-env http://alice@localhost:3000=SANDBOX_GITEA_TOKEN --git-identity
```

Statt einer Variable liest `--git-token-file URL=DATEI` den Token aus einer Datei, die Ihnen gehört, Modus
`0600` hat, kein Symlink ist und außerhalb aller Workspaces liegt.
`sandbox-wizard` legt solche Dateien unter `~/.config/sandbox-ai/tokens/` an,
wenn Sie dort einen Token eingeben (verdeckte Eingabe); die
Projektkonfiguration nennt dann nur die Datei, nie den Token.

**Vorhandene Git-Logins**: `--git-credentials DATEI` übernimmt die Logins aus
einer Datei im Format von Gits `store`-Credential-Helper, meist
`~/.git-credentials` (eine `https://USER:PASSWORT@HOST[:PORT]` pro Zeile).
Jedes Login antwortet für seinen Server genau wie eine `--git-token-*`-Option,
mit denselben Netzwerkregeln: Ein Login für `http://localhost:3000`
impliziert `--allow-host-port 3000`, eines für `https://github.com`
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
./claude-sandbox --git-credentials ~/.git-credentials --git-identity
```

## Lokale LLMs mit OpenCode

OpenCode kann einen lokalen **Ollama**-, **LM-Studio**- oder **vLLM**-Server
nutzen, ganz ohne Cloud-Zugriff. Am einfachsten geht das mit dem Wizard
(nächster Abschnitt); er listet die Modelle des Servers mit Name, Größe und
Ladezustand, jeweils sortierbar, und schreibt alles Folgende. Von Hand:

```bash
./sandbox-wizard models --kind ollama --host 192.168.1.20 --sort size
./opencode-sandbox --workspace "$HOME/src/demo" \
  --local-config ./opencode-local.json --allow-tcp 192.168.1.20:11434
```

`--local-config` kopiert die OpenCode-Konfiguration privat, entfernt alle
Standard-Cloud-Freigaben und deaktiviert Projektkonfiguration,
Modellkatalog-Abruf, Auto-Updates und Teilen. `--allow-tcp` öffnet genau den
Port des Servers. Die vom Wizard erzeugte Konfiguration nutzt OpenCodes
OpenAI-kompatiblen Provider (`http://IP:PORT/v1`).

| Server | Standardport | Modelle aus | Größe | Geladen |
|---|---|---|---|---|
| Ollama | 11434 | `/api/tags`, `/api/ps` | ja | ja |
| LM Studio | 1234 | `/api/v0/models` (Rückfall `/v1/models`) | falls gemeldet | ja |
| vLLM | 8000 | `/v1/models` | nein | immer (bereitgestellt = geladen) |

Die Sandbox erreicht `localhost` des Hosts nicht. Den Server ans LAN binden
(`OLLAMA_HOST=0.0.0.0`, in LM Studio „Serve on Local Network“,
`vllm serve … --host 0.0.0.0`) und die LAN-IP des Hosts verwenden.

## Lokale MCP-Server

Lokale MCP-Server laufen **in einer eigenen Sandbox** (Podman-Pod oder
Bubblewrap, gleicher Firejail-Guard), ohne Anmeldedaten und, sofern ihre
Definition nichts anderes sagt, ohne Netz. `--mcp NAME` startet die
Server-Sandbox, falls sie noch nicht läuft, und verbindet dann den Agenten; der
Server läuft für weitere Agenten und Sessions weiter.

```bash
./claude-sandbox --workspace "$HOME/src/app" --mcp demo   # eingebauter Testserver
./sandbox-mcp list                                        # NAME  STATUS  COMMAND
./sandbox-mcp stop demo
```

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
(Standard `codex`; jedes Image hat Node.js, Python und uv). Anmeldedaten,
Sessions, tmux, `--binary` und verschachteltes `--mcp` sind in einer Definition
nicht erlaubt. Ein Server, der sein Paket beim Start lädt (`npx`, `uvx`),
braucht die passende Registry-Freigabe. Pfade in einer Definition (`workspace`,
`--mount` ...) müssen absolut sein oder mit `~` beginnen: Ein laufender Server
wird von allen Agenten geteilt, die ihn nutzen.

## Projektkonfiguration und Wizard

```bash
./sandbox-wizard                  # interaktive Einrichtung, schreibt .sandbox-ai.json
./sandbox-wizard edit [VERZ]      # vorhandene Konfiguration eines Projekts ändern
./sandbox-wizard trust DATEI      # Konfiguration prüfen und vertrauen
./sandbox-wizard forget DATEI     # aus der Vertrauensliste entfernen
```

Der Wizard fragt Projektverzeichnis, weitere Workspaces, Agent, Backend, Netz
(oder für OpenCode ein lokales LLM), Modus ohne Rückfragen, GUI, tmux und Logs
ab. Das Netz ist `default` (nur der Anbieter des Agenten), `development`
(dazu PyPI, GitHub und die APIs von OpenAI und Anthropic), `frontier`, `dns`,
`egress-all` oder `offline`. Antworten lassen sich wie in einer Shell
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

**Bearbeiten.** `sandbox-wizard edit [VERZ|DATEI]` (oder `new` in einem Projekt
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
ignoriert sie. Optionen mit Wert auf der Kommandozeile haben Vorrang vor denen
der Datei, eine Auth-Wahl (`--copy-auth`, `--no-auth`, `--auth-file`,
`--import-codex-auth`) ersetzt die der Datei, und Agent-Argumente nach `--`
ersetzen die gespeicherten; einen Schalter, den die Datei setzt (z. B.
`--offline`), kann die Kommandozeile nicht aufheben, dafür `--no-config`
verwenden. **Eine Projektdatei wirkt
erst, wenn Sie ihr vertrauen:** Ein geklontes Repository könnte die Sandbox
sonst erweitern (`--egress-all-allow`, `--mount ~/.ssh:…`). Der SHA-256 jeder
vertrauten Datei liegt in `~/.config/sandbox-ai/trusted.json`; der Wizard
vertraut, was er schreibt, und jede Änderung verlangt erneut
`sandbox-wizard trust`, das genau die angezeigten Bytes vertraut. Die Datei
wird aus einem einzigen Lesevorgang gehasht und ausgewertet, muss eine reguläre
Datei sein und wird als Symlink abgelehnt. Das Löschen einer vertrauten Datei
hebt ihre Einschränkungen nicht still auf: Der Starter bricht mit einem Fehler
ab, bis sie wiederhergestellt ist, `--no-config` angegeben wird oder
`sandbox-wizard forget DATEI` sie aus der Vertrauensliste entfernt. Die Datei
darf weder `--workspace` (der Haupt-Workspace ist der Ort der Datei) noch
`--session`, `--config` oder interne Optionen setzen, und Optionsnamen müssen
ausgeschrieben sein (keine Abkürzungen). Pfade (`workspaces`, `--mount`,
`--state-dir`, Token-Dateien ...) müssen absolut sein oder mit `~` beginnen,
damit die Datei überall dasselbe bedeutet, egal wo der Starter aufgerufen wird.

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

## Sessions: lösen, pausieren, fortsetzen, stoppen

```bash
./claude-sandbox --workspace "$HOME/src/app" --session app    # starten und verbinden
# Ctrl-b d löst die Verbindung; der Agent arbeitet weiter, auch wenn das Terminal schließt.
./sandbox-session list
./sandbox-session attach app
./sandbox-session pause app      # alle Prozesse einfrieren
./sandbox-session resume app     # auftauen; oder eine gestoppte Session neu starten
./sandbox-session stop app       # stoppen; der Zustand bleibt
./sandbox-session delete app --yes
```

Eine Session führt den Starter als Supervisor im Hintergrund aus; in der
Sandbox hält eine gelöste tmux-Session das Fenster `agent` und das Fenster
`shell` (bash, gleiche Sandbox). Der Zustand liegt in
`~/.local/state/sandbox-ai/sessions/NAME/`: `home/` (dauerhaftes HOME),
`tmux/` (Socket), `session.json`, `supervisor.log`.

- **pause** friert den ganzen Podman-Pod ein (cgroup-Freezer) bzw. sendet unter
  Bubblewrap jedem Prozess des Sandbox-Baums SIGSTOP; **resume** taut auf.
- **stop** sendet dem Supervisor SIGTERM; er führt das normale Aufräumen aus,
  einschließlich des Zurückschreibens der Claude-Anmeldung. Workspace und HOME
  bleiben erhalten.
- **resume** einer gestoppten Session (oder ein erneuter Start mit
  `--session NAME`) startet sie mit den gespeicherten Optionen neu, und der
  Agent setzt sein letztes Gespräch fort: `claude --continue`,
  `codex resume --last`, `opencode --continue`. Mit `--binary` werden die
  ursprünglichen Argumente wiederverwendet.
  Die ursprünglichen Agent-Optionen (von der Kommandozeile, sonst die
  `agent_args` der Projektdatei, etwa `--model sonnet` oder Codex'
  `--sandbox read-only`) bleiben bis zum ersten Prompt vor den
  Fortsetzungs-Argumenten erhalten; Prompts, Einmal-Aufgaben (`-p`, `exec`,
  `run`) und Gesprächswahl (`--resume`) werden nicht wiederholt. Kam der erste
  Start nie hoch, werden die ursprünglichen Argumente erneut verwendet.
  Optionen und Agent-Argumente, die beim Neustart angegeben werden, wirken
  nicht; der Starter warnt und nennt sie. Für andere Optionen einen neuen
  Session-Namen verwenden. Die `.sandbox-ai.json` des Projekts wird bei jedem
  Start neu gelesen. Befehle, die eine Session ändern (Start, resume, pause,
  stop, delete), halten eine Sperre: Ein zweiter gleichzeitiger bricht mit
  einer Meldung ab, statt doppelt zu starten; ein zweiter Agent, der denselben
  MCP-Server startet, wartet und nutzt ihn dann mit.
- **Nach einem Absturz, `kill -9` oder Neustart des Rechners** konnte der
  Supervisor seinen Podman-Pod nicht entfernen. Das nächste resume entfernt
  ihn, aber nur, wenn der Pod das Label dieser Session trägt
  (`sandbox-ai.session`); jeden anderen Pod dieses Namens, etwa von einer
  Session unter einem anderen `XDG_STATE_HOME` oder aus einer Version vor dem
  Label, lässt es in Ruhe, und die Fehlermeldung nennt den Befehl
  `podman pod rm --force`.

Ein Stop überlebt `home/` und die Workspaces: Gespräche, Anmeldungen,
Einstellungen und alles, was unter `/root` installiert ist. Laufende Prozesse,
`/tmp`, der tmux-Verlauf und Dateien an anderen Stellen im schreibgeschützten
Container sind weg. „Letztes Gespräch“ heißt das jüngste in diesem HOME; ein
im Fenster `shell` von Hand gestartetes hat also Vorrang.

Beim Verbinden läuft nie ein tmux-Client auf dem Host: Der tmux-Server gehört dem
Agenten und könnte einem verbundenen Client Befehle zur Ausführung schicken.
Stattdessen führt ein Relay in der Sandbox, hinter Firejail, `tmux attach` an
einem eigenen Pseudo-Terminal aus, und `sandbox-session attach` tauscht über
`tmux/attach.sock` nur Terminal-Bytes und die Fenstergröße damit aus.
`--session` beinhaltet tmux und lässt sich nicht mit `--state-dir` oder
`--probe` kombinieren.

## tmux-Modus

tmux ist der Standard: Sind stdin und stdout ein Terminal, läuft der Agent in
tmux in der Sandbox mit den Fenstern `agent` und `shell`, beide hinter derselben
Firejail und Firewall. Endet der Agent, bleibt die Shell; mit dem letzten
Fenster endet die Sandbox. Es wird nur genutzt, wenn die Agent-Argumente reine
Optionen sind (`claude`, `claude --model opus`); ein Unterbefehl, Prompt oder
eine Ausgabe-und-Ende-Option (`claude -p`, `claude mcp list`, `codex exec`,
`opencode run`, `--version`) läuft ohne tmux, damit Ausgabe und Exit-Status
sichtbar bleiben. Aus bleibt es auch bei Probes, MCP-Servern, Sessions (schon
tmux), `--binary` und mit `--backend bwrap`, wenn der Host kein tmux hat. `--no-tmux` schaltet es ab,
`--tmux` besteht darauf (und scheitert ohne Terminal). Zum Lösen und
Wiederverbinden Sessions verwenden.

## GUI-Programme und Browser

| Option | Wirkung |
|---|---|
| `--gui` | Virtuelles X-Display `:99` (Xvfb); `DISPLAY` gesetzt, `/dev/shm` für Chromium vergrößert. |
| `--gui-screen BxHxT` | Bildschirmgröße, Standard `1920x1080x24`. |
| `--gui-vnc VERZ` | Impliziert `--gui`; VNC auf dem Unix-Socket `VERZ/vnc.sock` (Modus `0700`, kein TCP). |
| `--gui-wayland` | Wayland-Socket des Hosts freigeben: Fenster erscheinen auf Ihrem Desktop. |

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
./codex-sandbox --workspace "$HOME/src/app" --gui --egress-all-allow \
  --state-dir "$HOME/.local/state/sandbox-ai/pw" --binary /bin/bash
# darin: uvx --from playwright playwright install chromium
#        uv run --with playwright python mein_browsertest.py   # headless=False funktioniert
```

## Agenten ohne Rückfragen

`--dangerously-skip-permissions` startet den Agenten ohne Freigabedialoge:
Codex erhält `--dangerously-bypass-approvals-and-sandbox`, Claude
`--dangerously-skip-permissions` (plus `IS_SANDBOX=1`, das Claude als root
verlangt), OpenCode `OPENCODE_PERMISSION={"*":"allow"}`. Es entfallen nur die
Rückfragen des Agenten; Podman/Bubblewrap, Firejail und Firewall bleiben.

## Egress- und Dateizugriffs-Logs

`--log-dir VERZ` (Modus `0700`, außerhalb der Workspaces) schreibt JSON-Zeilen:

- `egress-denied.jsonl`: jedes verworfene Ziel (Adresse, TCP/UDP, Port; erstes
  Auftreten) und jede Auflösung eines nicht erlaubten Namens. Mit Logging gehen
  Anfragen an einen Stub auf `127.0.0.1:53` in der Sandbox, der den Namen
  protokolliert und NXDOMAIN antwortet.
- `files.jsonl`: `read`, `write`, `create`, `delete`, `move-from`, `move-to`
  in den Workspaces, `/root`, `/tmp` und allen Mounts (inotify; Wiederholungen
  je Pfad zwei Sekunden zusammengefasst). inotify liefert keine Prozess-ID;
  Lesezugriffe auf `/usr` und `/etc` werden nicht erfasst. Watches zählen gegen
  `fs.inotify.max_user_watches` des Host-Benutzers; ist das Limit erschöpft,
  wird ein Fehler protokolliert.

Die Sammler laufen im Supervisor außerhalb von Firejail; das Verzeichnis ist
für den Agenten ausgeblendet. Mit echtem DNS (`--dns`) werden Namen aufgelöst
und erscheinen nicht mehr als verweigerte Anfragen. Logs sind nicht
größenbegrenzt.

`sandbox-ai logs` (Standard: das Log-Verzeichnis des Wizards für das aktuelle
Projekt) zählt blockierte Ziele und Dateiereignisse und schlägt für jedes
blockierte Ziel die Option vor, die es freigeben würde: eine Vorlage für
bekannte Hosts, `--profile frontier` für Modell-APIs, `--allow-host` für
andere Namen, `--allow-tcp IP:PORT`, `--allow-host-port` für den Loopback
dieses Hosts oder `--allow IP`. Jeder Vorschlag erweitert die Sandbox, also
vorher prüfen. Namen und Pfade stammen vom Agenten: Sie werden mit allen
Steuerzeichen escaped angezeigt, und ein Name, der kein gültiger Hostname ist,
bekommt keinen Vorschlag.

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

## Optionsübersicht

`codex-sandbox`, `claude-sandbox` und `opencode-sandbox` haben alle folgenden
Optionen gemeinsam; `./claude-sandbox --help` zeigt sie ebenfalls. Argumente
nach `--` gehen unverändert an den Agenten. Mit *mehrfach* markierte Optionen
dürfen wiederholt werden.

### Die wichtigsten Optionen

| Option | Zweck |
|---|---|
| `--workspace DIR` | Projekt unter `/work` (Standard: aktuelles Verzeichnis) |
| `--session NAME` | Lösbare Session; Verwaltung mit `sandbox-session` |
| `--dangerously-skip-permissions` | Agent ohne Rückfragen; die Sandbox bleibt in Kraft |
| `--allow-github`, `--allow-pypi`, `--allow-host HOST` | Einzelne HTTPS-Ziele öffnen |
| `--allow-host-port PORT` | Dienst am Loopback des Hosts erreichen, etwa einen lokalen Git-Server |
| `--git-token-env URL=NAME`, `--git-token-file URL=DATEI`, `--git-identity` | Aus der Sandbox per HTTP(S) pushen, auch nach github.com |
| `--profile development` | Anbieter, PyPI, GitHub und die APIs von OpenAI und Anthropic |
| `--state-dir DIR` | Sandbox-HOME zwischen Läufen behalten |
| `--offline`, `--egress-all-allow` | Gar kein Netz / jedes ausgehende Ziel |
| `--gui`, `--gui-vnc DIR` | Virtuelles Display für GUI-Programme und Browser |
| `--log-dir DIR` | Blockierte Verbindungen und Dateizugriffe protokollieren |
| `--dry-run`, `--probe` | Plan zeigen / Isolation ohne Agent prüfen |

### Backend und Image

| Option | Bedeutung |
|---|---|
| `--backend podman\|bwrap` | Sandbox-Backend (Standard: `podman`) |
| `--image IMAGE` | Fertiges Podman-Image (Standard: `localhost/<agent>-sandbox:latest`); wird nie implizit gezogen |
| `--podman-url URL` | URL des Podman-Dienstes, z. B. `unix:///example/podman.sock` |
| `--binary PFAD` | Agent-Programm (Host-Pfad bei bwrap, Image-Pfad bei Podman) |
| `--runtime DIR` | bwrap: Laufzeitverzeichnis schreibgeschützt am selben Pfad einblenden; mehrfach |

Siehe [Agent starten](#agent-starten) und [Voraussetzungen](#voraussetzungen).

### Workspaces und Mounts

| Option | Bedeutung |
|---|---|
| `--workspace DIR[,DIR...]` | Erstes Verzeichnis unter `/work`, weitere unter `/workspaces/<name>`; mehrfach |
| `--workspace-ro` | Alle Workspaces schreibgeschützt einbinden |
| `--mount SRC:DEST:ro\|rw` | Zusätzlicher Mount; `DEST` unter `/work`, `/mnt` oder `/opt`; mehrfach |

Siehe [Workspaces und Mounts](#workspaces-und-mounts).

### Anmeldung, Zustand und Umgebung

| Option | Bedeutung |
|---|---|
| `--copy-auth` | Kopie der Anmeldedateien dieses Agenten verlangen (Login-Speicher, sonst Host); ohne die Option: kopiert, wenn vorhanden und das Sandbox-HOME noch kein Login hat |
| `--no-auth` | Keine Anmeldedaten des Hosts kopieren |
| `--auth-file DATEI` | Diese JSON-Datei an den Standard-Anmeldepfad des Agenten kopieren |
| `--import-codex-auth DATEI` | Nur OpenCode: eine Codex-ChatGPT-`auth.json` als private OAuth-Kopie importieren |
| `--copy SRC:ZIEL_RELATIV_ZU_HOME` | Einzelne Konfigurations- oder Anmeldedatei ins Sandbox-HOME kopieren; mehrfach |
| `--state-dir DIR` | Privates, dauerhaftes Sandbox-HOME (Modus 0700); Standard: nach dem Ende gelöscht |
| `--env NAME` | Eine Host-Variable nur an den Agenten geben; Werte werden nie ausgegeben; mehrfach |

Siehe [Anmeldung und Zustand](#anmeldung-und-zustand).

### Kits, Projekt-Memory und Plugins

| Option | Bedeutung |
|---|---|
| `--kit NAME` | Zusätzlich das Kit `~/.local/share/sandbox-ai/kits/NAME/AGENT`; wiederholbar |
| `--kit-dir DIR` | Zusätzlich das Kit in `DIR/AGENT`; wiederholbar |
| `--no-default-kit` | Ohne das Standard-Kit `~/.local/share/sandbox-ai/home/AGENT` |
| `--no-memory` | Kein Projekt-Memory (Memory-Datei und Claudes Auto-Memory werden weder geladen noch behalten) |
| `--no-plugins` | Keine Plugins aus dem Plugin-Store (Claude) bzw. den Kits (OpenCode) |

Siehe [Kits, Projekt-Memory und Plugins](#kits-projekt-memory-und-plugins).

### Netzwerk

| Option | Bedeutung |
|---|---|
| `--profile NAME` | `development`, `frontier`, `dns`, `egress-all` oder `offline`; mehrfach |
| `--frontier` | Zusätzlich API- und Anmelde-Hosts von OpenAI und Anthropic (TCP/443) |
| `--no-provider-defaults` | Standard-Hosts des Agenten weglassen |
| `--allow-pypi`, `--allow-github`, `--allow-gitlab`, `--allow-kernel-org`, `--allow-duckduckgo` | Host-Vorlagen (TCP/443) |
| `--allow-host HOST` | Hostnamen pinnen und TCP/443 erlauben; mehrfach |
| `--allow IP_ODER_CIDR` | Alle Protokolle und Ports zu diesem Ziel; mehrfach |
| `--allow-tcp IP:PORT` | Genau ein TCP-Port auf einer Adresse (IPv6: `[ADDR]:PORT`); mehrfach |
| `--allow-host-port PORT` | Podman: `127.0.0.1:PORT` in der Sandbox erreicht denselben Port am Host-Loopback; mehrfach |
| `--dns` | Echtes DNS über `1.1.1.1` und `8.8.8.8` (UDP/TCP 53 nur zu diesen) |
| `--nameserver IP` | Stattdessen diesen Resolver nutzen; impliziert `--dns`; mehrfach |
| `--egress-all-allow` | Jedes ausgehende Ziel erlauben; impliziert `--dns`; eingehend bleibt zu |
| `--offline` | Kein Netz nach außen, auch nicht zum Anbieter |
| `--network NAME`, `--ip IP`, `--ip6 IP` | Podman: vorhandenes privates Netz und feste Adressen |

Siehe [Netzwerkregeln](#netzwerkregeln).

### Git

| Option | Bedeutung |
|---|---|
| `--git-token-env URL=NAME` | Token aus der Host-Variable `NAME` für Git unter `URL` (`http(s)://[USER@]HOST[:PORT]`); Loopback-URLs implizieren `--allow-host-port`, andere `--allow-host`; mehrfach |
| `--git-token-file URL=DATEI` | Wie `--git-token-env`, Token aus einer privaten Datei (Modus `0600`, außerhalb der Workspaces); mehrfach |
| `--git-credentials DATEI` | Alle Logins aus `DATEI` (Format von Gits Credential-Store, z. B. `~/.git-credentials`; privat, außerhalb der Workspaces), jeweils mit den Netzwerkregeln von `--git-token-env` |
| `--git-identity` | Als globaler `user.name` und `user.email` des Hosts committen |
| `--privacy-check public\|always\|off` | Nach dem Lauf zeigen, was der nächste Push veröffentlichen würde und privat bleiben sollte: bei Workspaces mit öffentlichem Remote (Standard), bei jedem Git-Workspace oder nie; außer bei `off` warnt der `pre-commit`-Hook des Agenten und `pre-push` zu einem öffentlichen Remote verweigert |
| `--privacy-rules VERZ` | Eigene Regeln: `VERZ/global.rules.toml` und `VERZ/PROJEKT.rules.toml` (oder `.json`); außerhalb der Workspaces, nie für den Agenten sichtbar; Standard `~/.config/sandbox-ai/privacy`, falls vorhanden |

Siehe [Netzwerkregeln](#netzwerkregeln) (Pushen nach GitHub und auf einen eigenen Git-Server) und
[Datenschutzprüfung vor dem Veröffentlichen](#datenschutzprüfung-vor-dem-veröffentlichen).

### Agentenverhalten, MCP und lokale LLMs

| Option | Bedeutung |
|---|---|
| `--dangerously-skip-permissions` | Agent ohne Rückfragen starten |
| `--local-config DATEI` | OpenCode: rein lokale Konfiguration, Cloud-Zugriff abgeschaltet |
| `--mcp NAME` | Lokalen MCP-Server in eigener Sandbox anbinden; wird bei Bedarf gestartet; mehrfach |

Siehe [Agenten ohne Rückfragen](#agenten-ohne-rückfragen),
[Lokale LLMs](#lokale-llms-mit-opencode) und [Lokale MCP-Server](#lokale-mcp-server).

### Sessions, tmux und GUI

| Option | Bedeutung |
|---|---|
| `--session NAME` | Benannte, lösbare Session in tmux |
| `--tmux`, `--no-tmux` | tmux in der Sandbox (Fenster `agent` und `shell`) ist im Terminal Standard; `--tmux` besteht darauf, `--no-tmux` schaltet es ab |
| `--gui` | Virtuelles X-Display `:99` (Xvfb) |
| `--gui-screen BxHxTIEFE` | Größe des virtuellen Bildschirms (Standard: `1920x1080x24`) |
| `--gui-vnc DIR` | Impliziert `--gui`; VNC über den Unix-Socket `DIR/vnc.sock` |
| `--gui-wayland` | Wayland-Socket des Hosts freigeben (schwächt die Isolation) |

Siehe [Sessions](#sessions-lösen-pausieren-fortsetzen-stoppen), [tmux](#tmux-modus)
und [GUI](#gui-programme-und-browser).

### Konfiguration, Logs und Prüfungen

| Option | Bedeutung |
|---|---|
| `--config DATEI` | Projektkonfiguration (Standard: vertrauenswürdige `.sandbox-ai.json` im Haupt-Workspace) |
| `--no-config` | `.sandbox-ai.json` ignorieren |
| `--log-dir DIR` | `egress-denied.jsonl` und `files.jsonl` dorthin schreiben (für den Agenten unsichtbar) |
| `--dry-run` | Plan ausgeben; kein DNS, kein Lesen von Anmeldedaten, keine Kopien, Pods oder Prozesse |
| `--probe` | Beide Sandbox-Schichten und die Firewall prüfen, dann ohne Agent beenden |
| `-h`, `--help` | Vollständige Hilfe |
| `--version` | Version (jeder Befehl hat sie) |

Siehe [Projektkonfiguration](#projektkonfiguration-und-wizard) und
[Logs](#egress--und-dateizugriffs-logs).

### Hilfsbefehle

| Befehl | Argumente |
|---|---|
| `sandbox-ai BEFEHL …` | ein Befehl für alles Folgende, dazu `doctor`, `logs [VERZ\|--project P] [--json]`, `update [--check]`, `version` |
| `sandbox-ai privacy-check [PFAD ...]` | `[--rules-dir VERZ] [--project NAME] [--all] [--json]`: was der nächste Push jedes Repositorys veröffentlichen würde und privat bleiben sollte; Exit 1 bei blockierenden Funden, 2 wenn nicht prüfbar |
| `sandbox-build-image` | `[--podman-url URL] [--pin VERSION] [--uv VERSION] [--node IMAGE] AGENT\|all [podman-build-Optionen]`; neueste Versionen, sofern nicht fixiert |
| `sandbox-session list` | Sessions und ihr Status |
| `sandbox-session attach\|pause\|resume\|stop NAME` | Verbinden (lösen mit `Ctrl-b d`), einfrieren, fortsetzen bzw. neu starten, stoppen |
| `sandbox-session delete NAME [--yes]` | Gestoppte Session samt HOME entfernen |
| `sandbox-mcp list` | Definierte MCP-Server und ihr Status |
| `sandbox-mcp start NAME [--backend podman\|bwrap]`, `sandbox-mcp stop NAME` | MCP-Server-Sandbox starten oder stoppen |
| `sandbox-wizard [new]` | Interaktive Projekteinrichtung; bietet an, eine vorhandene Konfiguration zu bearbeiten |
| `sandbox-wizard edit [VERZ\|DATEI]` | Vorhandene Konfiguration eines Projekts ändern (Menü, Änderungen vor dem Schreiben) |
| `sandbox-ai login AGENT [Launcher-Optionen] [-- Login-Argumente]`, `--status`, `AGENT --logout` | Login-Speicher für Sandboxes |
| `sandbox-ai plugins [claude [--update] [Launcher-Optionen] [-- claude-plugin-Argumente]] [--status]` | Plugin-Store für Claude-Sandboxes |
| `sandbox-wizard trust DATEI [--yes]`, `sandbox-wizard forget DATEI` | Projektkonfiguration vertrauen bzw. von der Vertrauensliste entfernen |
| `sandbox-wizard models --kind ollama\|lmstudio\|vllm --host HOST [--port PORT] [--sort name\|size\|loaded]` | Modelle eines lokalen LLM-Servers auflisten |

## Tests

```bash
# Unit-, Contract-, Matrix- und End-to-End-Tests (startet keine Sandbox)
python3 -m pytest tests/test_agent_profiles.py tests/test_agent_sandbox.py \
  tests/test_agent_sandbox_{contracts,matrix,edges}.py tests/test_sessions.py tests/test_wizard.py \
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
echte Skript `sandbox-wizard` gegen einen lokalen Ollama-kompatiblen
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

## Fehlersuche

Zuerst `sandbox-ai doctor` ausführen: Es prüft alle Voraussetzungen und nennt
zu jedem Problem den Befehl, der es behebt.

| Symptom | Prüfen |
|---|---|
| Claude „not logged in“ / „Login expired · Please run /login“ | `sandbox-ai login --status` zeigt, welches Login die Sandboxes bekommen. `sandbox-ai login claude` gibt ihnen ein eigenes, unabhängig vom Host (empfohlen, wenn Claude auch auf dem Host läuft: Erneuern beide dasselbe Login, ist einer abgemeldet). Der Refresh braucht `console.anthropic.com`/`platform.claude.com`, standardmäßig erlaubt. |
| `Project configuration … is not trusted` | Datei prüfen, dann `./sandbox-wizard trust DATEI`, oder mit `--no-config` starten. |
| Lokales LLM nicht erreichbar | Server ans LAN gebunden, LAN-IP des Hosts verwendet, `--allow-tcp IP:PORT` passt. |
| `Unknown MCP server` / Server startet nicht | `./sandbox-mcp list`; `~/.local/state/sandbox-ai/mcp/NAME/supervisor.log` lesen. |
| `--tmux needs an interactive terminal` | `--tmux` weglassen (im Terminal ist tmux ohnehin Standard) oder für Hintergrundläufe `--session` verwenden. |
| `The trusted project configuration … is missing` | Datei wiederherstellen, mit `--no-config` starten oder `./sandbox-wizard forget DATEI`. |
| `… is a symlink; refusing` | Projektdateien und Sandbox-Anmeldedaten müssen reguläre Dateien sein; den Link ersetzen. |
| `inotify watch limit reached` | `fs.inotify.max_user_watches` erhöhen oder weniger Verzeichnisse beobachten. |
| Codex meldet UID-Mapping-Fehler | Ein anderer expliziter Codex-Sandbox-Modus aktiviert dessen eigene Sandbox wieder. |
| `newuidmap`/`newgidmap` schlägt fehl | Helfer, delegierte Bereiche, Hostpolicy prüfen. |
| Image fehlt | `./sandbox-build-image AGENT` oder `--image`. |
| Kopierziel existiert | Wiederholte Kopieroptionen bei wiederverwendetem `--state-dir` weglassen. |

## Grenzen und offene Arbeiten

1. **P1 – Pfad- und Zustandstrennung:** HOME oder `/` werden als Workspace nicht
   abgelehnt; gemeinsam genutzte Zustandsverzeichnisse haben keine Sperren.
2. **P2 – Live-Nachweise für neue Funktionen:** Sessions, tmux, GUI/VNC/Wayland,
   Projektkonfiguration, lokale LLMs, MCP-Server, Logs, DNS- und Egress-Modi sind durch
   Unit-, Contract- und End-to-End-Tests sowie neue Live-Tests abgedeckt, die
   noch auf echten Podman-/Bubblewrap-Hosts laufen müssen.
3. **P2 – Betriebsgrenzen:** keine Optionen für CPU-/RAM-/PID-Limits; Logs sind
   nicht größenbegrenzt.
4. **P2 – Claude-Lebenszyklus:** Token-Refresh und Zurückschreiben über eine
   lange Sitzung prüfen; die Kontozugehörigkeit zurückgeschriebener Tokens wird
   nicht geprüft.
5. **P3 – Reproduzierbare Builds:** Builds nehmen bewusst die neuesten
   Versionen; `--pin`, `--uv` und `--node` fixieren Agent, uv und Basis-Image,
   apt-Pakete sind nicht fixiert.

Bestandene Funktionstests sind kein Kernel-Escape-Audit und keine Garantie für
jede Host-Konfiguration. Firejail läuft mit `--noprofile` und den eigenen
Optionen des Starters; persönliche Firejail-Profile werden nicht geladen.
