# sandbox-ai: Schnellstart

[English](QUICKSTART.md) · [Ausführliche deutsche Anleitung](README_DE.md)

Alle Befehle im Verzeichnis `sandbox-ai/` als gewöhnlicher Benutzer ausführen.
Für den Standardweg werden rootless Podman und `pasta` benötigt.

## 1. Image bauen

```bash
./sandbox-build-image \
  --build-arg CODEX_VERSION=0.164.0 \
  --build-arg CLAUDE_VERSION=2.1.311 \
  --build-arg OPENCODE_VERSION=1.18.32
```

## 2. Isolation prüfen

```bash
./codex-sandbox --workspace "$HOME/src/sandbox-demo" --offline --probe
```

Bei Erfolg erscheint JSON mit `"verified": true`. Fehlende Workspace-
Verzeichnisse werden angelegt. Bei bestehenden erscheint eine Warnung und
es geht weiter. Der Workspace ist in der Sandbox unter `/work` sichtbar.

## 3. Agent starten

Nach Anmeldung des jeweiligen Agenten auf dem Host:

```bash
./codex-sandbox --workspace "$HOME/src/sandbox-demo"
./claude-sandbox --workspace "$HOME/src/sandbox-demo"
./opencode-sandbox --workspace "$HOME/src/sandbox-demo"
```

Vorhandene Standard-Auth-JSONs werden automatisch privat kopiert. Fehlen sie,
läuft der Start weiter. `--no-auth` deaktiviert die automatische Übernahme;
`--auth-file DATEI` wählt eine andere Datei. Ohne `--state-dir` wird das
Sandbox-Home nach Ende gelöscht.

Eine praktische Schreibanfrage mit Codex:

```bash
./codex-sandbox --workspace "$HOME/src/sandbox-demo" \
  -- exec --skip-git-repo-check --ephemeral 'Schreibe Hallo in hello.txt und lies die Datei zur Kontrolle.'
cat "$HOME/src/sandbox-demo/hello.txt"
```

Codex verwendet standardmäßig die geprüfte äußere Sandbox und Firejail;
seine zusätzliche native OS-Sandbox wird dafür mit `--sandbox danger-full-access`
deaktiviert. Der Workspace bleibt durch `--workspace-ro` äußerlich sperrbar.
`root:root` in der Sandbox ist das erwartete Mapping des Hostbenutzers.

OpenCode Cloud (`opencode.ai`) sowie OpenAI-/Anthropic-Endpunkte sind
standardmäßig für HTTPS freigegeben. Beispiel für das praktisch getestete
kostenlose Modell, sofern es weiterhin angeboten wird:

```bash
./opencode-sandbox --workspace "$HOME/src/sandbox-demo" --no-auth \
  -- run --pure --model opencode/glm-5.3-flash-free 'Schreibe Hallo in hello.txt und lies die Datei zur Kontrolle.'
```

Ein OpenCode-Modellkatalog muss verfügbar sein; ein vorhandener Hostkatalog
wird kopiert. Für einen Online-Katalogabruf `--allow-host models.dev` ergänzen.
Kosten und Verfügbarkeit vor späteren Aufrufen prüfen.

## 4. Zustand behalten

```bash
./codex-sandbox --workspace "$HOME/src/sandbox-demo" \
  --state-dir "$HOME/.local/state/sandbox-ai/codex"
```

Zustand außerhalb des Workspace halten. Vorhandener Zustand wird weitergenutzt,
Auth-Dateien werden nicht überschrieben. Für parallele Sitzungen getrennte
Zustandsverzeichnisse wählen.

## 5. Mounts und Netzwerk ergänzen

```bash
mkdir -p "$HOME/referenz" "$HOME/ausgabe"
./codex-sandbox --workspace "$HOME/src/sandbox-demo" \
  --mount "$HOME/referenz:/mnt/referenz:ro" \
  --mount "$HOME/ausgabe:/mnt/ausgabe:rw"
```

`--allow IP_ODER_CIDR` erlaubt alle Ports/Protokolle zu einem Ziel;
`--allow-host NAME` ergänzt HTTPS. `--no-provider-defaults` entfernt die
Standardfreigaben. `--offline` deaktiviert äußeres Netzwerk.

Für eine ausdrücklich lokale OpenCode-Modellkonfiguration:

```bash
# Beispieladresse durch die tatsächliche Modellserver-IP ersetzen.
./opencode-sandbox --workspace "$HOME/src/sandbox-demo" \
  --local-config "$HOME/.config/opencode/private-model.jsonc" --allow 192.0.2.10
```

Die Datei muss selbst lokale Provider konfigurieren. Dieser Modus deaktiviert
die Cloud-Standardfreigaben und kopiert die Konfiguration privat.
`127.0.0.1` innerhalb der Sandbox bezeichnet nicht den Host.

## Alternative: Bubblewrap

Mit installierten Host-CLIs und den [Voraussetzungen](README_DE.md#voraussetzungen):

```bash
./codex-sandbox --backend bwrap --workspace "$HOME/src/sandbox-demo" --offline --probe
./codex-sandbox --backend bwrap --workspace "$HOME/src/sandbox-demo"
./opencode-sandbox --backend bwrap --workspace "$HOME/src/sandbox-demo"
```

Ein Benutzerprofil für Firejail wird nicht zusätzlich geladen; der Starter
verwendet eigene geprüfte Optionen. Alle Optionen: `./codex-sandbox --help`.
Ein Startplan ohne Kopien oder Prozessstarts: `./codex-sandbox --dry-run`.
Praktische Prüfergebnisse: [Validierungsbericht](evidence/validation-summary.json).
