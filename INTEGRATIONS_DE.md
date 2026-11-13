# Integrationen: MCP-Server, Kits und Dienste

[English](INTEGRATIONS.md) · [README](README_DE.md)

Gewünschte Werkzeuge, an ihren Quellen geprüft (Release, Commit, Lizenz, was
sie wohin senden), und wo jedes in sandbox-ai steht. Drei Wege hinein, der
sicherste zuerst:

- **MCP-Server in einem eigenen Container** ([sandbox-ai-mcp](https://github.com/c0decave/sandbox-ai-mcp)):
  keine Capabilities, schreibgeschütztes Wurzelverzeichnis, Netz nur wie sein
  Eintrag sagt, Dateien nur über eingehängte Ordner, alles gepinnt. Die Sandbox
  des Agenten bleibt wie sie ist.
- **Kit** ([Kits](README_DE.md#kits-projekt-memory-und-plugins)): Skills, Plugins
  und Hooks für den Agenten selbst, in seiner Sandbox. Hooks laufen erst nach
  Ihrer Freigabe (`sandbox-ai kits trust-hooks KIT`).
- **Entfernter Dienst**: Der Agent spricht mit einem gehosteten MCP-Endpunkt.
  Das braucht seinen Host freigegeben (`--allow-host HOST`) und meist einen
  Schlüssel (`--secret-env NAME`); alles, was der Agent dorthin sendet, geht
  durch den Verbindungs-Proxy und wird protokolliert.

| Werkzeug | Lizenz | Geprüfte Version | Weg | Stand |
|---|---|---|---|---|
| [Playwright MCP](https://github.com/microsoft/playwright-mcp) | Apache-2.0 | 0.0.84 (`be26c080`), weiterhin die neueste | Container | vorbereitet (`candidates/playwright`), Debian-Pins gefunden, nicht gebaut |
| [Chrome DevTools MCP](https://github.com/ChromeDevTools/chrome-devtools-mcp) | Apache-2.0 | 1.12.0 (`af283441`) | Container, volles Netz | vorbereitet (`candidates/chrome-devtools`); der npm-Integritäts-Hash ist offen; Googles Nutzungsstatistik, CrUX-Abfragen und Update-Prüfung im Image abgeschaltet |
| [Context7](https://github.com/upstash/context7) | MIT | `@upstash/context7-mcp` 4.5.0 (`a138857c`) | Container, nur `context7.com:443`; oder entfernt `https://mcp.context7.com/mcp` | vorbereitet (`candidates/context7`), alle Pins gesetzt; `CONTEXT7_API_KEY` optional |
| [codebase-memory-mcp](https://github.com/DeusData/codebase-memory-mcp) | MIT | 0.12.1 (`a7158ac2`) | Container, kein Netz, der Workspace schreibgeschützt plus ein beschreibbarer Index | vorbereitet (`candidates/codebase-memory`), Release-Archiv per SHA-256 geprüft |
| [Browserbase MCP](https://github.com/browserbase/mcp-server-browserbase) | Apache-2.0 | 3.0.0 (`f6bd321a`) | Container, `api.browserbase.com`, der CDP-Host der Sitzung, Gemini; oder entfernt `https://mcp.browserbase.com/mcp` | vorbereitet (`candidates/browserbase`); braucht `BROWSERBASE_API_KEY`, `BROWSERBASE_PROJECT_ID`, `GEMINI_API_KEY` |
| [rea](https://github.com/morluto/rea) | MIT | 6.8.0 (`160d821a`) | Container, kein Netz | vorbereitet (`candidates/rea`), nur statische Analyse; Node ≥ 22.23 von nodejs.org ist offen; einige der ~230 Werkzeuge starten die Probe |
| [Superpowers](https://github.com/obra/superpowers) | MIT | 7.0.1 (`2ce8aa15`; 7.0.2 war jünger als 14 Tage) | Kit | **eingebaut**, mitgeliefert für Claude, Codex und OpenCode: `sandbox-ai claude --kit superpowers` (kein Plugin, kein Hook; das Plugin mit Hook bleibt als `superpowers@sandbox-ai`); dazu hallmark, playwright-skill, hyperframes, security-audit, design-system, ffuf und tdd: [docs/skills-review.md](docs/skills-review.md) |
| [OpenSpec](https://github.com/Fission-AI/OpenSpec) | MIT | 1.16.0 (`27c05157`) | Kit (eine CLI und Dateien pro Projekt) | eine Image-Erweiterung: das Release-Binary `openspec-linux-x64.tar.gz` (SHA-256 `f27fb2ff…2ae1`), dann `openspec init --tools claude,codex,opencode` im Projekt; seine Telemetrie ist mit `--telemetry off` (Standard) aus |
| [Serena](https://github.com/oraios/serena) | GPL-3.0-or-later | 2.1.0 (`5e4f5cd1`) | ein MCP-Server auf der Seite des Agenten | ändert das Projekt und hat ein Shell-Werkzeug, gehört also in die Sandbox des Agenten: `uv tool install -p 3.13 serena-agent==2.1.0`, `--enable-web-dashboard false`; Language-Server werden zur Laufzeit geladen (ihre Hosts freigeben) |
| [World Monitor](https://github.com/koala73/worldmonitor) | AGPL-3.0-only | 2.11.8 (`de602d00`) | entfernter Dienst `https://worldmonitor.app/mcp` | `--allow-host worldmonitor.app` und sein Schlüssel; die meisten Werkzeuge brauchen ein bezahltes Abo; selbst hosten heißt eine ganze Web-Anwendung |
| [MindsDB](https://github.com/mindsdb/mindsdb) | Elastic-2.0 (Kern), MIT (Integrationen) | 26.1.0 (`86be14b6`) | ein eigener Server, als entfernter Endpunkt erreicht | ein dauerhaft laufender Server mit Zustand (HTTP `:47334/mcp/streamable`), kein Container pro Verbindung; freigeben mit `--allow-tcp IP:47334`; sein einziges Werkzeug `query` führt beliebiges SQL auf den angebundenen Datenbanken aus |
| [ruflo](https://github.com/ruvnet/ruflo) | MIT | 3.71.4 (`bf5d71a1`) | nicht empfohlen | mehrere hundert MCP-Werkzeuge, darunter `terminal_execute` und npm-Installationen zur Laufzeit, Hooks bei jedem Werkzeugaufruf, ein Starter, der auf `npx …@latest` zurückfällt (ungepinnt), Föderations-Hosts; nur nach eigener Prüfung, als Kit |
| [Blender MCP](https://www.blender.org/lab/mcp-server/) | GPL-3.0-or-later (ungeprüft) | 1.0.0 (ungeprüft) | nur auf dem Host | braucht Blenders Oberfläche mit dem Add-on und führt beliebiges `bpy`-Python auf dem Host aus, über `localhost:9876`; eine Sandbox bräuchte `--allow-host-port 9876` und bekäme damit Ihr Blender |
| [screenpipe](https://github.com/screenpipe/screenpipe) | Screenpipe Commercial License | 2.7.138 (`41d15f18`) | ungeeignet | zeichnet Bildschirm und Ton des Hosts auf, also genau das, was eine Sandbox vom Agenten fernhält; die Lizenz schränkt kommerzielle Nutzung ein |

**Vorbereitet** heißt: Dockerfile, Prüfbericht (`PROVENANCE.md`) und Lizenz
liegen in `candidates/` von sandbox-ai-mcp, mit jedem Pin, der sich prüfen
ließ. Ein Kandidat wird zum Katalog-Eintrag, sobald er gebaut ist, sein
Smoke-Test in der CI besteht und der Digest seines Images veröffentlicht ist;
bis dahin bietet `sandbox-ai mcp add` ihn nicht an.
