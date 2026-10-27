# sandbox-ai

[English](README.md) · [Schnellstart](QUICKSTART_DE.md)

Linux-Starter für Codex, Claude Code und OpenCode mit zwei Sandbox-Schichten:

```text
Hostbenutzer → rootless Podman-Pod oder Bubblewrap → Firejail → Agent
```

`codex-sandbox`, `claude-sandbox` und `opencode-sandbox` verwenden dieselbe
Implementierung in `sandbox/agent_sandbox.py`. Standardbackend ist Podman.
Der Agent startet erst, wenn das innere Firejail zusätzliche Mount- und
PID-Namespaces, leere Capability-Masken, NoNewPrivs und Seccomp nachweist.
Bei fehlgeschlagener Prüfung wird der Start abgebrochen.

## Voraussetzungen

Als gewöhnlicher Hostbenutzer starten, ohne `sudo`. Benötigt werden Bash,
Python ab 3.9 und `readlink`.

- **Podman:** funktionierendes rootless Podman, delegierte UID-/GID-Bereiche,
  `pasta` und das vorab gebaute Image. Die Agenten und Firejail sind im Image.
- **Bubblewrap:** installierte Host-Agenten, `bwrap`, `firejail`, `nft`,
  `setpriv`, `newuidmap`, `newgidmap` und bei Netzwerkbetrieb `slirp4netns`.
  `/etc/subuid` und `/etc/subgid` müssen jeweils einen delegierten Bereich
  von mindestens 65536 IDs enthalten. Bei einer Installation unter NVM
  gegebenenfalls den Runtime-Pfad mit `--runtime` zusätzlich freigeben.

Kernel und Hostpolicy müssen die benötigten Namespaces, Helper und nftables
zulassen. Der Starter hebt solche Hostbeschränkungen nicht auf.

```bash
./sandbox-build-image \
  --build-arg CODEX_VERSION=0.164.0 \
  --build-arg CLAUDE_VERSION=2.1.311 \
  --build-arg OPENCODE_VERSION=1.18.32
./codex-sandbox --workspace "$HOME/src/demo" --offline --probe
./codex-sandbox --workspace "$HOME/src/demo"
```

Diese CLI-Versionen wurden im Prüflauf verwendet. Ohne Versionsargumente
verwendet das Buildrezept `latest`. Das Basisimage ist Node 22 auf Debian;
Basisimage und apt-Pakete sind noch nicht per Digest beziehungsweise Version
fixiert. Die Starter laden oder bauen kein Image automatisch.

## Workspace und Dateirechte

`--workspace PFAD` erscheint im Agenten als `/work` und ist standardmäßig
schreibbar. Relative Pfade beziehen sich auf das Aufrufverzeichnis.
Fehlende Verzeichnisse werden einschließlich Elternverzeichnissen angelegt.
Bei vorhandenen Verzeichnissen erscheint eine Warnung; Inhalt und Rechte
bleiben erhalten und der Start läuft weiter. `--dry-run` legt nichts an.
Dateipfade anstelle von Verzeichnissen und fehlende Rechte führen zu Fehlern.

```bash
./codex-sandbox --backend bwrap --workspace "$HOME/src/my_temp"
./codex-sandbox --workspace "$HOME/src/my_temp" --workspace-ro
```

`root:root` innerhalb der Sandbox ist bei diesen Startern die erwartete
Abbildung des gewöhnlichen Hostbenutzers auf Namespace-UID 0. Dafür ist kein
`chmod 777` nötig. Dateien unter `/work` erscheinen im gewählten Hostpfad.
Das flüchtige Sandbox-Home `/root` wird ohne `--state-dir` nach Ende entfernt.

**Codex:** Der Starter setzt standardmäßig `--sandbox danger-full-access`
für Codex, sobald es hinter den geprüften äußeren Schichten gestartet wird.
Codex verwendet damit diese äußere Isolation. Seine zusätzliche native
Sandbox scheiterte im praktischen Test am verschachtelten UID-Mapping
(`bwrap: setting up uid map: Operation not permitted`). Explizite Codex-
Optionen `--sandbox`, `-s` und `--dangerously-bypass-approvals-and-sandbox`
werden beibehalten. `--workspace-ro` bleibt ein äußerer, durch Codex nicht
aufhebbarer Read-only-Mount. Andere ausdrücklich gewählte Codex-Modi können
wiederum dessen native Sandbox aktivieren.

Zusätzliche Mounts sind wiederholbar:

```bash
mkdir -p "$HOME/referenz" "$HOME/ausgabe"
./opencode-sandbox --workspace "$HOME/src/demo" \
  --mount "$HOME/referenz:/mnt/referenz:ro" \
  --mount "$HOME/ausgabe:/mnt/ausgabe:rw"
```

Format: `QUELLE:ZIEL:ro|rw`. Quellen müssen existieren; Ziele liegen unter
`/work`, `/mnt` oder `/opt`. Pfade mit Leerzeichen quoten. Kommas und
unzulässige Ziele werden abgewiesen, ebenso einzelne Socket-/Gerätedateien.
Sockets innerhalb freigegebener Verzeichnisse können dennoch Hostdienste
zugänglich machen. Nur benötigte Projektverzeichnisse freigeben; HOME oder
`/` als Workspace würden entsprechend viele Hostdateien offenlegen.

Agentenargumente folgen nach `--`:

```bash
./codex-sandbox --workspace "$HOME/src/demo" \
  -- exec --skip-git-repo-check --ephemeral 'Lege hello.txt mit dem Inhalt Hallo an.'
```

## Auth-Kopien und Zustand

Ohne Auth-Option werden vorhandene Standard-Auth-Dateien des gewählten
Agenten automatisch privat kopiert. Fehlen sie, läuft der Starter weiter.
Bereits vorhandene Sandbox-Credentials bleiben erhalten.

| Agent | Standardquelle auf dem Host | Ziel relativ zum Sandbox-Home |
|---|---|---|
| Codex | `~/.codex/auth.json` | `.codex/auth.json` |
| Claude | `~/.claude/.credentials.json` | `.claude/.credentials.json` |
| Claude, zusätzliche Metadaten | `~/.claude.json` | `.claude.json` |
| OpenCode | `~/.local/share/opencode/auth.json` | `.local/share/opencode/auth.json` |

`CODEX_HOME`, `CLAUDE_CONFIG_DIR` und `XDG_DATA_HOME` werden bei der Suche
berücksichtigt. Claude-Metadaten werden zuerst im gewählten Konfigurations-
verzeichnis, danach unter HOME gesucht. OS-Keyring-Anmeldungen werden nicht
exportiert. Die Host-Originale werden nicht zurückgeschrieben.

- `--no-auth`: keine automatische Auth-Kopie; vorhandener Sandbox-Zustand bleibt.
- `--copy-auth`: Kopie ausdrücklich verlangen; fehlende primäre Auth-Datei oder
  belegtes Kopierziel sind Fehler.
- `--auth-file DATEI`: andere Auth-Datei an den Standardzielpfad kopieren.
- `--import-codex-auth DATEI`: nur OpenCode; Codex-ChatGPT-OAuth in eine private
  OpenCode-Auth-Kopie übersetzen. Die Anmeldung muss das Modell erlauben.
- `--copy QUELLE:RELATIVES_ZIEL`: zusätzliche Einzeldatei ins Sandbox-Home kopieren.

Ausdrücklich gewählte Auth-Quellen ersetzen die automatische Auswahl. Die
Auth-Auswahlschalter schließen sich gegenseitig aus. Dateien erhalten Modus
`0600`, private Verzeichnisse `0700`; vorhandene Kopierziele werden nicht
überschrieben. Ein vorhandener OpenCode-Modellkatalog wird ebenfalls kopiert,
sofern im Sandbox-Home noch keiner liegt. Andere Host-Konfigurationen werden
nicht pauschal übernommen.

```bash
./codex-sandbox --workspace "$HOME/src/demo" \
  --state-dir "$HOME/.local/state/sandbox-ai/codex"
./opencode-sandbox --workspace "$HOME/src/demo" \
  --import-codex-auth "$HOME/.codex/auth.json" \
  -- run --pure --model openai/gpt-6.1-sol 'Antworte mit OK'
```

Ohne `--state-dir` wird das Home nach Ende gelöscht. Ein vorhandenes
Zustandsverzeichnis muss dem Benutzer gehören und Modus `0700` haben.
Zustand **außerhalb des Workspace** ablegen und für parallele Sitzungen
getrennte Verzeichnisse verwenden. Erneuerte Sandbox-Tokens bleiben bei
persistenter Ablage dort; sie werden nicht in die Host-Auth-Datei kopiert.

## Netzwerk und OpenCode Cloud

Ein eigener Netzwerk-Namespace und nftables begrenzen den Verkehr.
Standardmäßig werden folgende Namen beim Start aufgelöst, in `/etc/hosts`
festgeschrieben und als Ziel-IP-Adressen für **TCP/443** freigegeben:

```text
api.openai.com    chatgpt.com    auth.openai.com
api.anthropic.com    claude.ai    claude.com    platform.claude.com
```

Für **OpenCode zusätzlich: `opencode.ai`**. Die OpenCode Cloud ist damit im
normalen Modus freigegeben. Die bloße Ablage einer Konfiguration auf dem Host
macht sie nicht zu einer lokalen Modellkonfiguration. Für ausschließlich
eigene Modellserver muss der lokale Modus ausdrücklich gewählt werden:

```bash
# Dokumentationsadresse: durch die tatsächliche Modellserver-IP ersetzen.
./opencode-sandbox --workspace "$HOME/src/demo" \
  --local-config "$HOME/.config/opencode/private-model.jsonc" \
  --allow 192.0.2.10
```

`--local-config` kopiert die gewählte OpenCode-Konfiguration privat, entfernt
alle Cloud-Standardfreigaben und deaktiviert Projektkonfiguration, Modell-
katalogabruf, Auto-Update und Sharing über Agenten-Umgebungsvariablen.
Die Datei muss selbst ausschließlich die gewünschten lokalen Provider und
Modelle konfigurieren. Explizite `--allow`/`--allow-host` bleiben möglich.
Ein bei Wiederverwendung abweichender Konfigurationsinhalt wird nicht
stillschweigend über eine vorhandene Datei geschrieben.

| Option | Wirkung |
|---|---|
| `--allow IP_ODER_CIDR` | Alle IP-Protokolle und Ports zu diesem Ziel; wiederholbar, IPv4/IPv6. |
| `--allow-host NAME` | Name auflösen, IP-Adressen festschreiben, TCP/443 freigeben. |
| `--no-provider-defaults` | Alle Provider-Standardfreigaben entfernen. |
| `--offline` | Kein äußeres Netzwerk; mit zusätzlichen Netzoptionen unvereinbar. |
| `--network NAME` | Bereits bestehendes privates Podman-Netz verwenden. |
| `--ip ADRESSE`, `--ip6 ADRESSE` | Feste Adresse innerhalb des gewählten Podman-Netzes. |

```bash
# Nur Beispieladressen; an das bestehende Netz anpassen.
./codex-sandbox --workspace "$HOME/src/demo" \
  --network research --ip 198.51.100.10 --allow 198.51.100.0/24
```

Es gibt keine allgemeine DNS-Freigabe. Beispielsweise muss `models.dev` für
einen gewünschten Online-Katalogabruf zusätzlich mit `--allow-host` freigegeben
werden. Ein mitkopierter Katalog kann ohne diesen Abruf genutzt werden.
Bei Provider-IP-Wechseln neu starten. Die Firewall unterscheidet keine Domains
auf gemeinsam genutzten CDN-IP-Adressen. Loopback, Antwortverkehr und nötige
IPv6-Nachbarschaftserkennung sind zugelassen; `127.0.0.1` bezeichnet die
Sandbox. Ein Host-Modellserver braucht eine aus deren Netz erreichbare Adresse.

Podman verwendet standardmäßig `pasta`, Bubblewrap `slirp4netns` mit gesperrtem
Host-Loopback. Es werden keine Ports veröffentlicht. `--podman-url` wählt bei
Bedarf einen Podman-Dienst, beispielsweise
`--podman-url "unix://${XDG_RUNTIME_DIR}/podman/podman.sock"`.
Bind-Quellen müssen auf dem Engine unter denselben Pfaden vorhanden sein.
Das ist keine automatische Dateiübertragung zu einem anderen Rechner.

## Weitere Optionen

| Option | Zweck |
|---|---|
| `--backend podman\|bwrap` | Äußere Sandbox wählen; Standard Podman. |
| `--image NAME` | Vorhandenes Podman-Image wählen. |
| `--binary PFAD` | Anderes Programm starten; Codex-Standardargumente werden dann nicht ergänzt. |
| `--runtime VERZEICHNIS` | Bubblewrap: zusätzliche Runtime unter gleichem Pfad read-only bereitstellen. |
| `--env NAME` | Gewählte Hostvariable nur dem Agenten übergeben; Werte stehen nicht im Startplan. |
| `--dry-run` | Plan ohne DNS-Auflösung, Auth-Lesen, Verzeichniserstellung oder Sandbox-Start. |
| `--probe` | Firewall und beide Sandbox-Schichten prüfen, danach ohne Agent beenden. |
| `--help` | Vollständige CLI-Hilfe. |

## Bewertung der vorgeschlagenen Profile

Der Starter lädt mit `firejail --noprofile` eigene Optionen. Eine Datei
`~/.config/firejail/opencode.profile` wird dadurch nicht automatisch verwendet.
Das vorgeschlagene Profil enthält sinnvolle Maßnahmen wie Seccomp und
Capability-Entzug, braucht für diesen Aufbau aber Anpassungen:

- `protocol unix,inet,inet6` filtert Socket-Familien; es ersetzt keine
  Ziel-IP-/Port-Firewall.
- HOME-Whitelists müssen Workspace, native Programm-/Runtime-Pfade und bei
  Persistenz auch `.local/state/opencode` berücksichtigen.
- `private-cache` erzeugt einen flüchtigen Cache; die gewünschte persistente
  Cache-Ablage braucht eine dazu passende Policy.
- `disable-mnt` und `disable-write-mnt.inc` können die hier vorgesehenen
  Workspace-/Mountpfade sperren. `blacklist /usr/libexec` kann Toolchain-Helfer
  treffen. Die Wirkung hängt auch von den eingebundenen lokalen Profilen ab.
- `restrict-namespaces` verhindert weitere Namespace-Erzeugung und kann
  entsprechende Tools blockieren. `noroot` passt nicht ungeprüft zu einem
  inneren Start als Namespace-UID 0.

Das vorgeschlagene Bubblewrap-Skript verwendet mit `--share-net` das Hostnetz,
erbt ohne `--clearenv` die Umgebung und macht mit dem kompletten `/etc` mehr
Hostdaten lesbar. Direkt gebundene Auth-/Datenverzeichnisse sind keine privaten
Kopien. Der abschließende direkte OpenCode-Aufruf startet selbst kein Firejail.
Die Starter hier verwenden stattdessen private Auth-Kopien, eine ausgewählte
Umgebung, ein eigenes Netz und einen überprüften inneren Firejail-Start.

## Verifikation und Fehlersuche

```bash
python3 -m pytest tests/test_agent_sandbox.py tests/test_agent_sandbox_{contracts,matrix,edges}.py -q
python3 -m pytest tests/test_agent_sandbox_live.py --agent-live   -k 'real_probe or real_auth_copy_and_mount_permissions' -q

# Vier echte Modelltests: Codex und kostenloses OpenCode-Modell, beide Backends.
python3 -m pytest tests/test_agent_sandbox_live.py \
  --agent-live --agent-provider-tests -k practical_agent_tools -q
```

Bestanden: 314 Unit-Fälle, 64 Untertests und 22 reale Fälle, darunter vier
praktische Modellprüfungen. Die Unit-Tests enthalten Happy-, Sad- und Edge-
Verträge für alle 35 Funktionen.
Ein zusätzlicher Test meldet fehlende Verträge, sobald neue Funktionen hinzukommen.
Die Matrix kombiniert drei Agenten, zwei Backends, fünf Auth-Modi und neue/bestehende
Workspaces. Weitere Matrizen prüfen Netzwerkregeln, Zustand, Fehler beim Start,
Pipe-Erzeugung und Bootstrap. [Testmatrix mit Ergebnissen](evidence/test-matrix.json).
Die Regressionen für Mount-Überlagerungen und unvollständige Pipe-Erzeugung
wurden zuerst rot gemessen und danach behoben:
[Regressionsnachweise](evidence/regressions.json).

Die Befehle gelten aus `sandbox-ai/`. Reale Auth-Kopiertests benötigen die
JSON-Anmeldungen aller drei Agenten. Die praktischen Modelltests benötigen
Codex-Auth und einen OpenCode-Katalog unter `~/.cache/opencode/models.json`;
OpenCode verwendet dafür keine Auth-Kopie. Codex verursacht reguläre Nutzung.
`--agent-backend podman|bwrap`, `--codex-model` und `--free-model` grenzen die
Prüfung ein beziehungsweise wählen Modelle. Das OpenCode-Modell muss im
Katalog Ein-/Ausgabekosten von 0 haben; zusätzlich prüft der Test die gemeldeten
Kosten. Die Verfügbarkeit kostenloser Modelle kann sich ändern.

Die praktischen Prüfungen lesen eine Datei mit zufälligen, im Prompt nicht
enthaltenen Werten, schreiben das korrekte Ergebnis und prüfen es per
Shell-Werkzeug. Anschließend werden Dateien auf dem Host und Tool-Ereignisse
kontrolliert. Ein erfolgreicher CLI-Exit oder eine Textantwort allein reicht
nicht. Ergebnisse: [Validierungsbericht](evidence/validation-summary.json)
und die vier Einzelberichte in [evidence/](evidence/).
Eine Claude-Modellanfrage ist dadurch nicht nachgewiesen.

| Fehlerbild | Prüfen |
|---|---|
| `bwrap: setting up uid map: Operation not permitted` in Codex | Aktuellen Starter verwenden; eine ausdrücklich aktivierte zusätzliche Codex-Sandbox kann wieder scheitern. |
| `/work` bleibt leer | Hostpfad und Aufrufverzeichnis prüfen; Schreibtest muss nach `/work`, nicht ins flüchtige Home gehen. |
| `root:root` in der Sandbox | Erwartetes UID-Mapping; Hostrechte prüfen, nicht pauschal erweitern. |
| `newuidmap`/`newgidmap` scheitert | Helper, delegierte Bereiche und Hostpolicy prüfen. |
| Image fehlt | `sandbox-build-image` ausführen oder passendes `--image` wählen. |
| HTTP 401 | Anmeldung beziehungsweise Token-Refresh prüfen. |
| Modell unbekannt | Konfiguration, Berechtigung und Modellkatalog prüfen. |
| Kopierziel existiert | Wiederholte ausdrückliche Kopieroption weglassen oder getrennten Zustand verwenden. |

## Offene Arbeiten nach Dringlichkeit

1. **P1 – Pfad- und Zustandstrennung härten:** HOME oder `/` als Workspace
   werden noch nicht technisch gesperrt; `--state-dir` innerhalb des Workspace
   ebenfalls nicht. Dadurch können Credentials versehentlich im Projekt landen.
   Für gemeinsam verwendete Zustandsverzeichnisse fehlen Sperren; parallele
   Änderungen und Symlink-Rennen sind noch nicht umfassend abgesichert.
2. **P2 – Betriebsgrenzen und Fehlerpfade:** CLI-Optionen für CPU/RAM/PID-Limits
   und zusätzliche reale Nachweise für erzwungene Abbrüche und gleichzeitige
   Sitzungen fehlen. Unit-Tests prüfen Cleanup und Abbruchpfade.
3. **P2 – Agenten-Lebenszyklus:** Claude-Modellanfrage mit Dateischreiben sowie
   Token-Refresh und erneuter Start mit erneuerten Credentials praktisch prüfen.
4. **P3 – Reproduzierbare Builds:** Basisimage-Digest und Systempakete fixieren;
   getestete CLI-Versionen bei Updates erneut validieren.

Das sind verbleibende Grenzen; die bestandenen Funktionstests sind kein
Kernel-Escape-Audit und keine Garantie für beliebige Host-Konfigurationen.
