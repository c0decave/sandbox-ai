# sandbox-ai: Schnellstart-Tutorial

[English](QUICKSTART.md) · [Vollständige Dokumentation](README_DE.md)

In etwa fünfzehn Minuten bauen Sie die Images, prüfen die Isolation, lassen
einen Agenten an einem Projekt arbeiten, richten das Projekt mit dem Wizard ein
und arbeiten mit einer lösbaren Session. Optionale Schritte zeigen ein lokales
LLM und Browser-Tests.

Alle Befehle als normaler Benutzer ausführen, nie mit `sudo`. Das
Standard-Backend braucht funktionierendes rootless Podman mit `pasta`;
`podman info --format '{{.Host.Security.Rootless}}'` sollte `true` ausgeben.
Unter macOS laufen die Sandboxes in einer rootless `podman machine`, die
`/Users` freigibt (`sandbox-ai doctor` prüft das; die README sagt, was dort
bisher geht).

## 0. Installieren

```bash
./install.sh --from .     # aus einem Checkout; später: git pull && ./install.sh --from .
sandbox-ai doctor         # was auf diesem Host fehlt und wie man es behebt
```

Das legt die Befehle in `~/.local/bin` (und `~/.bin`, falls vorhanden), dieses
Verzeichnis in `~/.bashrc` und `~/.zshrc` auf Ihren `PATH` (`--no-modify-rc` lässt
beide in Ruhe) und die Dateien in `~/.local/share/sandbox-ai` ab. Öffnen Sie danach ein
neues Terminal. Ohne `--from` lädt es das neueste
Release (`--source URL` wählt das Repository). Alles funktioniert auch direkt
aus dem Checkout als `./sandbox-ai claude` usw., wie unten; nur darf der Checkout
selbst nie ein Workspace sein. Die Tab-Vervollständigung für bash, zsh und fish
kommt mit (`sandbox-ai completion install` richtet sie erneut ein).

Ohne Checkout: Die [Release-Seite](https://github.com/c0decave/sandbox-ai/releases)
bietet `sandbox-ai.pyz` (eine Datei, jedes Python ab 3.9), ein AppImage für
Linux (x86_64, aarch64) und `sandbox-ai.app` für macOS, fertig zum Starten
([Vier Wege zur Installation](README_DE.md#vier-wege-zur-installation)). Mit der
`.pyz` lauten die Befehle unten `./sandbox-ai.pyz build all` usw.

## 1. Images bauen

```bash
./sandbox-ai build all
```

Das baut `localhost/codex-sandbox`, `localhost/claude-sandbox` und
`localhost/opencode-sandbox` mit den neuesten Agent-Versionen auf der aktuellen
Node.js-LTS. Nur Benötigtes bauen: `./sandbox-ai build claude`.
Derselbe Befehl aktualisiert später.

## 2. Isolation prüfen

```bash
./sandbox-ai claude --workspace "$HOME/src/sandbox-demo" --offline --probe
```

Erwartet ist JSON mit `"verified": true`: äußere Sandbox, Firejail und Firewall
sind aktiv. Fehlt das Workspace-Verzeichnis, wird es angelegt.

## 3. Einen Agenten starten

Den Sandboxes ein Login geben. Entweder auf dem Host anmelden (`claude auth
login`, `codex login` oder `opencode auth login`): Die Sandboxes bekommen eine
Kopie. Oder, besser, wenn der Agent auch auf dem Host läuft, den Sandboxes ein
eigenes Login geben, in einer Sandbox erstellt und für alle aufbewahrt:

```bash
sandbox-ai login claude          # angezeigten Link öffnen, Code einfügen
sandbox-ai login --status        # welches Login die Sandboxes jedes Agenten bekommen
```

Skills, Agents und Commands für die Sandboxes gehören ins Standard-Kit
`~/.local/share/sandbox-ai/home/AGENT/` (beim ersten Start mit einem Beispiel-Skill
angelegt); das Memory des Agents bleibt pro Projekt erhalten. Für Claude-Plugins,
darunter die Demo `/sandcastle`, einmal `sandbox-ai plugins claude` ausführen.

Dann:

```bash
./sandbox-ai claude --workspace "$HOME/src/sandbox-demo"
cd ~/src/sandbox-demo && sandbox-ai claude   # dasselbe: das Verzeichnis, in dem Sie starten, ist /work
```

Die Startzeilen sagen, welcher Ordner `/work` ist und welche Projektkonfiguration
gilt, was in die Sandbox kopiert wurde und, für Claude, wie lange die Anmeldung
gültig ist:

```text
agent-sandbox: /work is /home/ich/src/sandbox-demo; configuration: none
agent-sandbox: auth copied: /home/ich/.claude/.credentials.json -> /root/.claude/.credentials.json
agent-sandbox: auth copied: /home/ich/.claude.json -> /root/.claude.json
agent-sandbox: Claude access token valid for 7.4 h
```

In der Sandbox ist das Projekt `/work`; alles, was der Agent dort schreibt,
erscheint in `~/src/sandbox-demo`. Der Agent erreicht nur seinen Provider. Eine
einmalige Aufgabe ausprobieren:

```bash
./sandbox-ai codex --workspace "$HOME/src/sandbox-demo" \
  -- exec --skip-git-repo-check 'Schreibe Hallo in hello.txt und lies die Datei zur Kontrolle.'
cat "$HOME/src/sandbox-demo/hello.txt"
```

Sehen, was passieren würde, ohne etwas zu starten? `--dry-run` anhängen.

## 4. Das Projekt mit dem Wizard einrichten

```bash
./sandbox-ai wizard
```

```text
Project directory (mounted at /work) [/home/ich/src/sandbox-demo]:
  Security modes (options can only narrow the chosen one):
    lax: every outbound destination (--egress-all-allow), logged with --log-debug; no approval questions
    …
Security mode (lax/low/medium/high/paranoid) [medium]:
Additional workspaces, comma-separated (mounted at /workspaces/<name>): ~/src/shared-lib
Agent (codex/claude/opencode): claude
Backend (podman/bwrap) [podman]:
  default: the agent's own provider only; development: plus PyPI, GitHub and the OpenAI and Anthropic APIs; …
Network (default/development/frontier/dns/egress-all/offline) [default]: development
Advanced: choose the sandbox's own network addresses (--subnet; default: the backend's own) [y/N]:
Start the agent without permission prompts (--dangerously-skip-permissions) [y/N]: y
Virtual display for GUI apps and browsers (--gui) [y/N]:
Run in tmux with an extra shell window (default of the launcher in a terminal; no adds --no-tmux) [Y/n]:
Log denied egress and file access (--log-dir) [y/N]: y
Allow further network destinations (presets such as github, hosts, TCP ports, ports on this host) [y/N]:
Set up Git (commit identity, push tokens for GitHub or your own Git server) [y/N]: y
Commit as your host's git user.name and user.email (--git-identity) [Y/n]:
Token for github.com (…): enter it now, use a host variable, a secret of `sandbox-ai secrets`, or none (none/enter/variable/secret) [none]:
Add a push token for this project's origin https://git.example.org [y/N]: y
Token for https://git.example.org: enter it now, use a host variable or a secret (enter/variable/secret) [enter]:
Token for https://git.example.org (input hidden, empty to skip):
Your own Git server (Gitea, Forgejo, GitLab ...) for a token, e.g. (…), or a Git credentials file (…), empty to finish:
Keep these tokens out of the sandbox: the connection proxy adds them to Git's requests, the agent never reads them (--token-proxy; no GH_TOKEN for gh then) [Y/n]:
  Kit: /home/ich/.local/share/sandbox-ai/home/claude (…); project memory on. Change both later with: sandbox-ai wizard edit, part k
Further options (read-only workspaces, persistent HOME, mounts, variables, MCP, display, any other) [y/N]:
…
Write configuration [Y/n]:
Wrote the token to /home/ich/.config/sandbox-ai/tokens/https-git.example.org (mode 0600; the project configuration names only this file)
Wrote and trusted /home/ich/src/sandbox-demo/.sandbox-ai.json
Start with: sandbox-ai claude --workspace /home/ich/src/sandbox-demo
```

Antworten lassen sich wie in einer Shell bearbeiten (Backspace, Pfeiltasten,
Tab ergänzt Pfade). Zuerst kommt der Sicherheitsmodus: Enter behält `medium`
(oder den Modus Ihrer Vorgaben), und die Fragen danach bieten nur an, was der
Modus erlaubt (`high` und `paranoid` öffnen nichts über den Anbieter des
Agenten hinaus; README, „Sicherheitsmodi“). `development` erlaubt PyPI, GitHub
und die APIs von OpenAI und Anthropic. Die letzten drei Blöcke sind optional;
mit `n` überspringen. Der
Git-Block bietet einen Token für das `origin`-Remote des Projekts an (mit `y`
bestätigen; Vorgabe ist Nein, weil der Agent dieses Remote ändern kann). Ein
eingegebener Token wird nicht angezeigt und landet in einer privaten Datei
(Modus 0600) außerhalb des Projekts; die Konfiguration nennt nur diese Datei.
Mit `variable` bleibt der Token stattdessen in einer Host-Variable, mit `secret`
in einem Geheimnis von `sandbox-ai secrets`. Für HTTPS-Server bietet der Git-Block
auch `--token-proxy` an: Der Verbindungs-Proxy setzt den Token in die Anfragen von
Git ein, und der Agent liest ihn nie.

Ab jetzt wendet der Starter diese Einstellungen automatisch an (auch bei
`cd ~/src/sandbox-demo && sandbox-ai claude`: Ein Verzeichnis mit Projektdatei ist
immer `/work`); Optionen auf der Kommandozeile haben weiterhin Vorrang. Später ändern Sie die Konfiguration
mit `sandbox-ai wizard edit ~/src/sandbox-demo`: Es zeigt die Teile als Menü,
fragt mit den aktuellen Werten als Vorgabe erneut und zeigt vor dem Schreiben,
was sich ändert. Ändert sich die Datei oder klonen
Sie ein Projekt, das eine enthält: prüfen und mit `./sandbox-ai wizard trust DATEI`
bestätigen. Eine nicht vertraute Datei wird nie angewendet.

## 5. In einer Session arbeiten, die Sie verlassen und fortsetzen können

```bash
./sandbox-ai claude --workspace "$HOME/src/sandbox-demo" --name demo
```

Sie sind jetzt mit tmux in der Sandbox verbunden: Fenster `agent` führt Claude
aus, Fenster `shell` ist eine bash in derselben Sandbox (`Ctrl-b n` wechselt).
`Ctrl-b d` löst die Verbindung; der Agent arbeitet weiter, auch wenn Sie das
Terminal schließen. Jeder Start funktioniert so; ohne `--name` bekommt er einen
Codenamen wie `brisk-otter-lantern` (der Start gibt ihn aus, `sandbox-ai list`
zeigt ihn mit seinem Arbeitsordner).

```bash
./sandbox-ai list                  # NAME  KIND  AGENT  STATE  HOME  STARTED (UTC)  WORKSPACE
./sandbox-ai attach demo           # wieder verbinden
./sandbox-ai pause demo            # alles einfrieren, z. B. um die CPU freizugeben
./sandbox-ai resume demo           # genau dort weitermachen
./sandbox-ai stop demo             # beenden; Dateien und HOME bleiben
./sandbox-ai resume demo           # später: eine neue Sandbox, Claude setzt das Gespräch fort
```

## 6. Optional: ein lokales LLM mit OpenCode

Den Server im LAN erreichbar machen, zum Beispiel für Ollama:
`OLLAMA_HOST=0.0.0.0 ollama serve` (LM Studio: „Serve on Local Network“,
vLLM: `--host 0.0.0.0`). Dann den Wizard starten, `opencode` wählen, die Frage
nach dem lokalen LLM mit `y` beantworten, den Servertyp wählen, die LAN-IP des
Hosts angeben (nicht `127.0.0.1`), die Liste sortieren und ein Modell wählen:

```text
#  NAME               SIZE    LOADED  DETAILS
1  qwen2.5-coder:14b  9.0 GB  yes     14.8B Q4_K_M
2  llama3.2:3b        2.0 GB  no      3.2B Q4_K_M
```

OpenCode spricht danach nur mit diesem Server; sonst verlässt nichts die
Sandbox. Modelle ohne Wizard auflisten:
`./sandbox-ai wizard models --kind ollama --host IP`.

**Für jeden Agenten und für Server anderswo** (ein anderer Rechner, ein VPN,
das Internet, mit API-Schlüssel): den Server einmal anlegen und mit `--llm`
starten. Der Schlüssel bleibt in `sandbox-ai secrets`; der Agent sieht nur
einen Platzhalter.

```bash
sandbox-ai llm add gpu --kind ollama --url http://192.168.1.20:11434
sandbox-ai llm check gpu --agent claude      # Claude braucht Ollama 0.14 oder neuer
./sandbox-ai claude --llm gpu:qwen2.5-coder:14b --no-provider-defaults
```

Auch der Wizard bietet angelegte Server an. Einzelheiten: README_DE,
„LLM-Server: lokal und entfernt“.

## 7. Optional: GUI-Programme und Browser-Tests

```bash
mkdir -p "$HOME/.local/state/sandbox-ai/vnc-demo"
./sandbox-ai codex --workspace "$HOME/src/sandbox-demo" --egress-all-allow \
  --gui-vnc "$HOME/.local/state/sandbox-ai/vnc-demo" --binary /bin/bash
# darin:
uvx --from playwright playwright install chromium
uv run --with playwright python -c "
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch(headless=False); pg = b.new_page()
    pg.goto('https://example.org'); pg.screenshot(path='/work/shot.png'); b.close()"
```

Währenddessen das Display mit
`vncviewer ~/.local/state/sandbox-ai/vnc-demo/vnc.sock` beobachten.
`--egress-all-allow` ist hier nur für den Browser-Download nötig.

## 8. Optional: ein MCP-Server in eigener Sandbox

```bash
./sandbox-ai claude --workspace "$HOME/src/sandbox-demo" --mcp demo \
  -- -p 'Rufe das version-Tool des MCP-Servers demo auf und zeige das Ergebnis.'
./sandbox-ai mcp list
./sandbox-ai mcp stop demo
```

Der eingebaute Server `demo` läuft in einer eigenen Sandbox und antwortet mit
seiner Version und Umgebung; `list_directory` listet sein eigenes `/work`.
Echte Server in `~/.config/sandbox-ai/mcp.json` definieren (siehe README).

## 9. Optional: die Logs ansehen

Mit eingeschaltetem Logging (Wizard-Frage oder `--log-dir VERZ`):

```bash
cd ~/src/sandbox-demo && sandbox-ai logs   # gesperrte Ziele mit der passenden Freigabe, Dateizugriffe
```

Die rohen JSON-Zeilen liegen als `egress-denied.jsonl` und `files.jsonl` im
Log-Verzeichnis unter `~/.local/state/sandbox-ai/logs/`.

## 10. Optional: mit einem Preset starten

```bash
sandbox-ai presets list                          # Analyse, Entwicklung, Tests, Spaß
sandbox-ai presets show software-analysis        # was es setzt und was es fragt
sandbox-ai claude --workspace "$HOME/src/sandbox-demo" \
  --preset software-analysis --preset-value samples=~/samples
sandbox-ai demo-device start                     # ein emuliertes serielles Gerät auf 127.0.0.1:4000
sandbox-ai claude --workspace "$HOME/src/sandbox-demo" --preset hardware-analysis
```

Ein Preset besteht aus Optionen, MCP-Servern und einer ersten Aufgabe; die
Kommandozeile gewinnt gegen es. `sandbox-ai presets check NAME` nennt die
MCP-Server, die noch hinzuzufügen sind.

## 11. Optional: während des Laufs etwas ändern

```bash
sandbox-ai net allow --allow-host pypi.org       # mehr Ziele, bis die Sandbox endet
sandbox-ai net allow --allow-host-port 3080      # ein Dienst auf diesem Host, über ein Relay
sandbox-ai net allow --git-token-env http://localhost:3080=SANDBOX_GIT_TOKEN   # ein Git-Login
sandbox-ai net allow --allow-host api.osv.dev --keep   # auch für die nächsten Starts
sandbox-ai net show                              # was dazukam
```

Noch besser: Bewahren Sie Ihre Git-Logins außerhalb jeder Sandbox auf. Der
Verbindungs-Proxy fügt sie in Gits Anfragen ein, und kein Token kommt je in eine
Sandbox:

```bash
sandbox-ai secrets set GITEA_TOKEN
sandbox-ai git-logins add http://localhost:3080 --secret GITEA_TOKEN --user sie
```

API-Schlüssel gehen genauso: Mit `--api-key dev.to=DEVTO_API_KEY` ist
`$DEVTO_API_KEY` beim Agenten nur ein Platzhalter, und der Proxy setzt den
gespeicherten Schlüssel erst auf der Leitung ein, nur für HTTPS an diesen Host.
Für npm, PyPI, crates.io, GitLab und GitHub: `sandbox-ai publish-logins add pypi
--secret PYPI_TOKEN`, dann mit `--publish pypi` starten; jede Veröffentlichung wird
vorher erfragt. Mehrere Konten (ein öffentliches, eines für die Arbeit, keines
für eine Recherche) sind Identitäten: `sandbox-ai identities add NAME ...`, dann
`--identity NAME`.

Nahe der Speichergrenze warnt der Launcher Sie, und der Agent kann
`/sandbox/session/memory.json` lesen; bei 95 % beendet er den größten Test oder
Build, damit die Sandbox überlebt. `sandbox-ai web` bietet all das als Webseite
(Passwort und Einmalcode), auch für andere Rechner über SSH (`sandbox-ai hosts`).

## Die wichtigsten Optionen

| Option | Zweck |
|---|---|
| `--workspace DIR` | Projekt unter `/work`; für weitere wiederholen oder mit Kommas trennen |
| `--name NAME` | Den Start benennen (siehe Schritt 5); `--no-keep` verwirft sein HOME am Ende |
| `--mode high` | Sicherheitsmodus: `lax`, `low`, `medium` (Vorgabe), `high`, `paranoid`; Optionen können ihn nur enger machen |
| `--dangerously-skip-permissions` | Keine Rückfragen; die Sandbox bleibt in Kraft |
| `--allow-github`, `--allow-pypi`, `--allow-host HOST` | Einzelne HTTPS-Ziele öffnen |
| `--allow-host-port PORT` | Dienst auf dem eigenen Host erreichen, etwa einen lokalen Git-Server (während des Laufs: `net allow --allow-host-port`) |
| `--model opus`, `--effort high` | Modell und Gründlichkeit des Agenten, in seinen eigenen Worten (Claude Code, Codex) |
| `--preset NAME`, `--preset-value K=V` | Mit einem Preset starten (siehe Schritt 10) |
| `--git-token-env URL=NAME`, `--git-identity` | Aus der Sandbox pushen, z. B. `http://localhost:3080=SANDBOX_GIT_TOKEN` |
| `--git-token-env https://github.com=NAME` | Nach GitHub pushen |
| `--git-credentials ~/.git-credentials` | Vorhandene Git-Logins nutzen (der Agent kann sie lesen) |
| `--kit NAME`, `--no-memory` | Skills, Agents und Commands eines Kits dazunehmen (`~/.local/share/sandbox-ai/kits/NAME`); ohne Projekt-Memory starten |
| `--profile development` | Anbieter, PyPI, GitHub und die APIs von OpenAI und Anthropic |
| `--state-dir DIR` | Ein eigenes Sandbox-HOME für mehrere Starts (jeder Start behält ohnehin sein eigenes) |
| `--offline`, `--egress-all-allow` | Kein Netz / jedes ausgehende Ziel |
| `--gui`, `--gui-vnc DIR` | Virtuelles Display (siehe Schritt 7) |
| `--log-dir DIR` | Logs (siehe Schritt 9) |
| `--dry-run`, `--probe` | Plan zeigen / Isolation prüfen |

Auf ein lokales Gitea oder Forgejo auf Port 3080 pushen:

```bash
export SANDBOX_GIT_TOKEN=...   # Gitea: Einstellungen -> Anwendungen
./sandbox-ai claude --workspace "$HOME/src/sandbox-demo" \
  --git-token-env http://localhost:3080=SANDBOX_GIT_TOKEN --git-identity
```

`./sandbox-ai claude --help` listet jede Option; die
[Optionsübersicht](README_DE.md#optionsübersicht) der README fasst alle
zusammen, dazu `sandbox-ai list`, `sandbox-ai mcp`, `sandbox-ai wizard` und
`sandbox-ai build`.

## Weiter

Die [vollständige Dokumentation](README_DE.md) erklärt jede Option:
Netzwerkmodi, mehrere Workspaces, Anmeldung und den Claude-Token-Abgleich,
Bubblewrap, Wayland und die Testsuiten.
