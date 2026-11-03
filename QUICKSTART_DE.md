# sandbox-ai: Schnellstart-Tutorial

[English](QUICKSTART.md) · [Vollständige Dokumentation](README_DE.md)

In etwa fünfzehn Minuten bauen Sie die Images, prüfen die Isolation, lassen
einen Agenten an einem Projekt arbeiten, richten das Projekt mit dem Wizard ein
und arbeiten mit einer lösbaren Session. Optionale Schritte zeigen ein lokales
LLM und Browser-Tests.

Alle Befehle als normaler Benutzer ausführen, nie mit `sudo`. Das
Standard-Backend braucht funktionierendes rootless Podman mit `pasta`;
`podman info --format '{{.Host.Security.Rootless}}'` sollte `true` ausgeben.

## 0. Installieren

```bash
./install.sh --from .     # aus einem Checkout; später: git pull && ./install.sh --from .
sandbox-ai doctor         # was auf diesem Host fehlt und wie man es behebt
```

Das legt die Befehle in `~/.local/bin` (und `~/.bin`, falls vorhanden) und die
Dateien in `~/.local/share/sandbox-ai` ab. Ohne `--from` lädt es das neueste
Release (`--source URL` wählt das Repository). Alles funktioniert auch direkt
aus dem Checkout als `./claude-sandbox` usw., wie unten; nur darf der Checkout
selbst nie ein Workspace sein.

## 1. Images bauen

```bash
./sandbox-build-image all
```

Das baut `localhost/codex-sandbox`, `localhost/claude-sandbox` und
`localhost/opencode-sandbox` mit den neuesten Agent-Versionen auf der aktuellen
Node.js-LTS. Nur Benötigtes bauen: `./sandbox-build-image claude`.
Derselbe Befehl aktualisiert später.

## 2. Isolation prüfen

```bash
./claude-sandbox --workspace "$HOME/src/sandbox-demo" --offline --probe
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
./claude-sandbox --workspace "$HOME/src/sandbox-demo"
```

Die Startzeilen zeigen, was in die Sandbox kopiert wurde, und für Claude, wie
lange die Anmeldung gültig ist:

```text
agent-sandbox: auth copied: /home/ich/.claude/.credentials.json -> /root/.claude/.credentials.json
agent-sandbox: auth copied: /home/ich/.claude.json -> /root/.claude.json
agent-sandbox: Claude access token valid for 7.4 h
```

In der Sandbox ist das Projekt `/work`; alles, was der Agent dort schreibt,
erscheint in `~/src/sandbox-demo`. Der Agent erreicht nur seinen Provider. Eine
einmalige Aufgabe ausprobieren:

```bash
./codex-sandbox --workspace "$HOME/src/sandbox-demo" \
  -- exec --skip-git-repo-check 'Schreibe Hallo in hello.txt und lies die Datei zur Kontrolle.'
cat "$HOME/src/sandbox-demo/hello.txt"
```

Sehen, was passieren würde, ohne etwas zu starten? `--dry-run` anhängen.

## 4. Das Projekt mit dem Wizard einrichten

```bash
./sandbox-wizard
```

```text
Project directory (mounted at /work) [/home/ich/src/sandbox-demo]:
Additional workspaces, comma-separated (mounted at /workspaces/<name>): ~/src/shared-lib
Agent (codex/claude/opencode): claude
Backend (podman/bwrap) [podman]:
  default: the agent's own provider only; development: plus PyPI, GitHub and the OpenAI and Anthropic APIs; …
Network (default/development/frontier/dns/egress-all/offline) [default]: development
Start the agent without permission prompts (--dangerously-skip-permissions) [y/N]: y
Virtual display for GUI apps and browsers (--gui) [y/N]:
Run in tmux with an extra shell window (default of the launcher in a terminal; no adds --no-tmux) [Y/n]:
Log denied egress and file access (--log-dir) [y/N]: y
Allow further network destinations (presets such as github, hosts, TCP ports, ports on this host) [y/N]:
Set up Git (commit identity, push tokens for GitHub or your own Git server) [y/N]: y
Commit as your host's git user.name and user.email (--git-identity) [Y/n]:
Token for github.com (…): enter it now, use a host variable, or none (none/enter/variable) [none]:
Add a push token for this project's origin http://localhost:3000 [y/N]: y
Token for http://localhost:3000: enter it now or use a host variable (enter/variable) [enter]:
Token for http://localhost:3000 (input hidden, empty to skip):
Your own Git server (Gitea, Forgejo, GitLab ...) for a token, e.g. http://localhost:3000, empty to finish:
Further options (read-only workspaces, persistent HOME, mounts, variables, MCP, display, any other) [y/N]:
…
Write configuration [Y/n]:
Wrote the token to /home/me/.config/sandbox-ai/tokens/http-localhost-3000 (mode 0600; the project configuration names only this file)
Wrote and trusted /home/ich/src/sandbox-demo/.sandbox-ai.json
Start with: ./claude-sandbox --workspace /home/ich/src/sandbox-demo
```

Antworten lassen sich wie in einer Shell bearbeiten (Backspace, Pfeiltasten,
Tab ergänzt Pfade). `development` erlaubt PyPI, GitHub und die APIs von OpenAI
und Anthropic. Die letzten drei Blöcke sind optional; mit `n` überspringen. Der
Git-Block bietet einen Token für das `origin`-Remote des Projekts an (mit `y`
bestätigen; Vorgabe ist Nein, weil der Agent dieses Remote ändern kann). Ein
eingegebener Token wird nicht angezeigt und landet in einer privaten Datei
(Modus 0600) außerhalb des Projekts; die Konfiguration nennt nur diese Datei.
Mit `variable` bleibt der Token stattdessen in einer Host-Variable.

Ab jetzt wendet der Starter diese Einstellungen automatisch an; Optionen auf
der Kommandozeile haben weiterhin Vorrang. Später ändern Sie die Konfiguration
mit `sandbox-wizard edit ~/src/sandbox-demo`: Es zeigt die Teile als Menü,
fragt mit den aktuellen Werten als Vorgabe erneut und zeigt vor dem Schreiben,
was sich ändert. Ändert sich die Datei oder klonen
Sie ein Projekt, das eine enthält: prüfen und mit `./sandbox-wizard trust DATEI`
bestätigen. Eine nicht vertraute Datei wird nie angewendet.

## 5. In einer Session arbeiten, die Sie verlassen und fortsetzen können

```bash
./claude-sandbox --workspace "$HOME/src/sandbox-demo" --session demo
```

Sie sind jetzt mit tmux in der Sandbox verbunden: Fenster `agent` führt Claude
aus, Fenster `shell` ist eine bash in derselben Sandbox (`Ctrl-b n` wechselt).
`Ctrl-b d` löst die Verbindung; der Agent arbeitet weiter, auch wenn Sie das
Terminal schließen.

```bash
./sandbox-session list            # NAME  AGENT  BACKEND  STATUS  …
./sandbox-session attach demo     # wieder verbinden
./sandbox-session pause demo      # alles einfrieren, z. B. um die CPU freizugeben
./sandbox-session resume demo     # genau dort weitermachen
./sandbox-session stop demo       # beenden; Dateien und HOME bleiben
./sandbox-session resume demo     # später: Neustart, Claude setzt das Gespräch fort
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
`./sandbox-wizard models --kind ollama --host IP`.

## 7. Optional: GUI-Programme und Browser-Tests

```bash
mkdir -p "$HOME/.local/state/sandbox-ai/vnc-demo"
./codex-sandbox --workspace "$HOME/src/sandbox-demo" --egress-all-allow \
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
./claude-sandbox --workspace "$HOME/src/sandbox-demo" --mcp demo \
  -- -p 'Rufe das version-Tool des MCP-Servers demo auf und zeige das Ergebnis.'
./sandbox-mcp list
./sandbox-mcp stop demo
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

## Die wichtigsten Optionen

| Option | Zweck |
|---|---|
| `--workspace DIR` | Projekt unter `/work`; für weitere wiederholen oder mit Kommas trennen |
| `--session NAME` | Lösbare Session (siehe Schritt 5) |
| `--dangerously-skip-permissions` | Keine Rückfragen; die Sandbox bleibt in Kraft |
| `--allow-github`, `--allow-pypi`, `--allow-host HOST` | Einzelne HTTPS-Ziele öffnen |
| `--allow-host-port PORT` | Dienst auf dem eigenen Host erreichen, etwa einen lokalen Git-Server |
| `--git-token-env URL=NAME`, `--git-identity` | Aus der Sandbox pushen, z. B. `http://localhost:3000=SANDBOX_GIT_TOKEN` |
| `--git-token-env https://github.com=NAME` | Nach GitHub pushen |
| `--git-credentials ~/.git-credentials` | Vorhandene Git-Logins nutzen (der Agent kann sie lesen) |
| `--kit NAME`, `--no-memory` | Skills, Agents und Commands eines Kits dazunehmen (`~/.local/share/sandbox-ai/kits/NAME`); ohne Projekt-Memory starten |
| `--profile development` | Anbieter, PyPI, GitHub und die APIs von OpenAI und Anthropic |
| `--state-dir DIR` | Sandbox-HOME zwischen Läufen behalten |
| `--offline`, `--egress-all-allow` | Kein Netz / jedes ausgehende Ziel |
| `--gui`, `--gui-vnc DIR` | Virtuelles Display (siehe Schritt 7) |
| `--log-dir DIR` | Logs (siehe Schritt 9) |
| `--dry-run`, `--probe` | Plan zeigen / Isolation prüfen |

Auf ein lokales Gitea oder Forgejo auf Port 3000 pushen:

```bash
export SANDBOX_GIT_TOKEN=...   # Gitea: Einstellungen -> Anwendungen
./claude-sandbox --workspace "$HOME/src/sandbox-demo" \
  --git-token-env http://localhost:3000=SANDBOX_GIT_TOKEN --git-identity
```

`./claude-sandbox --help` listet jede Option; die
[Optionsübersicht](README_DE.md#optionsübersicht) der README fasst alle
zusammen, dazu `sandbox-session`, `sandbox-mcp`, `sandbox-wizard` und
`sandbox-build-image`.

## Weiter

Die [vollständige Dokumentation](README_DE.md) erklärt jede Option:
Netzwerkmodi, mehrere Workspaces, Anmeldung und den Claude-Token-Abgleich,
Bubblewrap, Wayland und die Testsuiten.
