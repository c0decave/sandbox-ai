"""The app's texts in English and German: tr("English text", **values) gives the text in the chosen
language, with {placeholders} filled in. No Qt.

The language comes from the app's settings ("system", "en" or "de"); "system" means
$SANDBOX_AI_APP_LANGUAGE, else the locale ($LC_ALL, $LC_MESSAGES, $LANG, $LANGUAGE): German for de_*,
English otherwise. A key without an entry is shown as it is (in English); tests make sure there is none.
"""
from __future__ import annotations

import os

LANGUAGES = ("en", "de")
_language = "en"


def N_(text):  # noqa: N802 - the usual name for "translated later"
    """Marks TEXT as translatable where it is defined (tables of titles); tr() translates it when shown."""
    return text


def system_language(environ=None):
    environ = os.environ if environ is None else environ
    if environ.get("SANDBOX_AI_APP_LANGUAGE") in LANGUAGES:
        return environ["SANDBOX_AI_APP_LANGUAGE"]
    for name in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
        value = environ.get(name)
        if value:
            return "de" if value.split(":")[0].lower().startswith("de") else "en"
    return "en"


def set_language(code):
    """Use CODE ("en", "de" or "system") from now on; returns the language chosen."""
    global _language
    _language = code if code in LANGUAGES else system_language()
    return _language


def language():
    return _language


def tr(key, **values):
    entry = TEXTS.get(key)
    text = entry.get(_language) or entry["en"] if entry else key
    return text.format(**values) if values else text


TEXTS = {
    # sandboxes: the other hosts' starts beside this account's own
    'A command runs.': {
        'en': 'A command runs.',
        'de': 'Ein Befehl läuft.'},
    'Not a question of sandbox-ai: {ident}': {
        'en': 'Not a question of sandbox-ai: {ident}',
        'de': 'Keine Frage von sandbox-ai: {ident}'},
    "Not a start's name: {ident}": {
        'en': "Not a start's name: {ident}",
        'de': 'Kein Name eines Starts: {ident}'},
    'this machine': {
        'en': 'this machine',
        'de': 'dieser Rechner'},
    '{count} on other hosts': {
        'en': '{count} on other hosts',
        'de': '{count} auf anderen Hosts'},
    'Not while it is {state} on {host}.': {
        'en': 'Not while it is {state} on {host}.',
        'de': 'Nicht, solange es auf {host} {state} ist.'},
    'End the selected start on {host} (sandbox-ai stop there)': {
        'en': 'End the selected start on {host} (sandbox-ai stop there)',
        'de': 'Den gewählten Start auf {host} beenden (dort sandbox-ai stop)'},
    'On {host}: {text}': {
        'en': 'On {host}: {text}',
        'de': 'Auf {host}: {text}'},
    'On {host}: delete session {name} and its HOME?': {
        'en': 'On {host}: delete session {name} and its HOME?',
        'de': 'Auf {host}: Session {name} und ihr HOME löschen?'},
    'On {host}': {
        'en': 'On {host}',
        'de': 'Auf {host}'},
    # hosts: the pane names the host it shows
    'Host: {host}': {
        'en': 'Host: {host}',
        'de': 'Host: {host}'},
    '{host}: no answer': {
        'en': '{host}: no answer',
        'de': '{host}: keine Antwort'},
    # extra preset directories (sandbox-ai presets dirs)
    'Add folder…': {
        'en': 'Add folder…',
        'de': 'Ordner hinzufügen…'},
    'This sandbox-ai has no extra preset directories yet; update it.': {
        'en': 'This sandbox-ai has no extra preset directories yet; update it.',
        'de': 'Dieses sandbox-ai kennt noch keine zusätzlichen Vorlagen-Ordner; aktualisieren Sie es.'},
    'Cannot list the preset directories: {error}': {
        'en': 'Cannot list the preset directories: {error}',
        'de': 'Die Vorlagen-Ordner lassen sich nicht auflisten: {error}'},
    '{path}  ({count} presets)': {
        'en': '{path}  ({count} presets)',
        'de': '{path}  ({count} Vorlagen)'},
    '{path}  (skipped: {why})': {
        'en': '{path}  (skipped: {why})',
        'de': '{path}  (übergangen: {why})'},
    'Presets are read from your folder {yours} first, then from these in their order, then the built-in ones. A folder sandboxes can write is refused.': {
        'en': 'Presets are read from your folder {yours} first, then from these in their order, then the built-in ones. A folder sandboxes can write is refused.',
        'de': 'Vorlagen werden zuerst aus Ihrem Ordner {yours} gelesen, dann aus diesen in ihrer Reihenfolge, dann die eingebauten. Ein Ordner, in den Sandboxen schreiben können, wird abgelehnt.'},
    'Folder with presets (*.json)': {
        'en': 'Folder with presets (*.json)',
        'de': 'Ordner mit Vorlagen (*.json)'},
    'Preset directories': {
        'en': 'Preset directories',
        'de': 'Vorlagen-Ordner'},
    'From the preset directory {path}': {
        'en': 'From the preset directory {path}',
        'de': 'Aus dem Vorlagen-Ordner {path}'},
    # Tour about…: tours and Help sections by topic
    'Tour about…': {
        'en': 'Tour about…',
        'de': 'Tour zu…'},
    'Type a topic: the tours and Help sections about it, best first (Ctrl+F1)': {
        'en': 'Type a topic: the tours and Help sections about it, best first (Ctrl+F1)',
        'de': 'Geben Sie ein Thema ein: die Touren und Hilfeabschnitte dazu, die besten zuerst (Strg+F1)'},
    'Tour through a task': {
        'en': 'Tour through a task',
        'de': 'Tour durch eine Aufgabe'},
    'Tour of a page': {
        'en': 'Tour of a page',
        'de': 'Tour einer Seite'},
    'Help section': {
        'en': 'Help section',
        'de': 'Hilfeabschnitt'},
    'Which topic? A tour shows it step by step on the pages; a Help section explains it.': {
        'en': 'Which topic? A tour shows it step by step on the pages; a Help section explains it.',
        'de': 'Welches Thema? Eine Tour zeigt es Schritt für Schritt auf den Seiten; ein Hilfeabschnitt erklärt es.'},
    'A topic, e.g. network, presets, kits, Netzwerk, Vorlagen': {
        'en': 'A topic, e.g. network, presets, kits, Netzwerk, Vorlagen',
        'de': 'Ein Thema, z. B. Netzwerk, Vorlagen, Kits, network, presets'},
    'Start the tour': {
        'en': 'Start the tour',
        'de': 'Tour starten'},
    'Open the section': {
        'en': 'Open the section',
        'de': 'Abschnitt öffnen'},
    'Nothing about {text}: try another word, in English or German.': {
        'en': 'Nothing about {text}: try another word, in English or German.',
        'de': 'Nichts zu {text}: Versuchen Sie ein anderes Wort, auf Deutsch oder Englisch.'},
    '{count} found; Enter starts the first.': {
        'en': '{count} found; Enter starts the first.',
        'de': '{count} gefunden; Enter startet den ersten.'},
    # kits: adding into a kit of yours from any selection
    'Add to a kit of yours': {
        'en': 'Add to a kit of yours',
        'de': 'Zu einem eigenen Kit hinzufügen'},
    'A new kit…': {
        'en': 'A new kit…',
        'de': 'Ein neues Kit…'},
    "New kit's name": {
        'en': "New kit's name",
        'de': 'Name des neuen Kits'},
    '{why} What you add goes to a kit of yours: you choose it (or a new one) first.': {
        'en': '{why} What you add goes to a kit of yours: you choose it (or a new one) first.',
        'de': '{why} Was Sie hinzufügen, kommt in ein eigenes Kit: Sie wählen es (oder ein neues) zuerst.'},
    'No kit of yours is selected.': {
        'en': 'No kit of yours is selected.',
        'de': 'Kein eigenes Kit ausgewählt.'},
    'Declare {plugin} in a kit of yours:': {
        'en': 'Declare {plugin} in a kit of yours:',
        'de': '{plugin} in einem eigenen Kit eintragen:'},
    # >>> canary log (Activity, tab Canaries) >>>
    'unknown ({why})': {
        'en': 'unknown ({why})',
        'de': 'unbekannt ({why})'},
    'unknown': {
        'en': 'unknown',
        'de': 'unbekannt'},
    ', {pid} in the sandbox': {
        'en': ', {pid} in the sandbox',
        'de': ', {pid} in der Sandbox'},
    'Tool call ({match}): {summary}  [{time}, activity {id}]': {
        'en': 'Tool call ({match}): {summary}  [{time}, activity {id}]',
        'de': 'Tool-Aufruf ({match}): {summary}  [{time}, Aktivität {id}]'},
    'Prompt turn: {summary}  [{time}, activity {id}]': {
        'en': 'Prompt turn: {summary}  [{time}, activity {id}]',
        'de': 'Prompt-Zug: {summary}  [{time}, Aktivität {id}]'},
    'activity {id}': {
        'en': 'activity {id}',
        'de': 'Aktivität {id}'},
    'Model request ({position}): {request}  [{time}, {where}]': {
        'en': 'Model request ({position}): {request}  [{time}, {where}]',
        'de': 'Modellanfrage ({position}): {request}  [{time}, {where}]'},
    'Also held open by: {process}': {
        'en': 'Also held open by: {process}',
        'de': 'Auch offen gehalten von: {process}'},
    'Canary': {
        'en': 'Canary',
        'de': 'Canary'},
    'Process': {
        'en': 'Process',
        'de': 'Prozess'},
    'same second': {
        'en': 'same second',
        'de': 'dieselbe Sekunde'},
    'carried the token': {
        'en': 'carried the token',
        'de': 'trug das Token'},
    'names the file': {
        'en': 'names the file',
        'de': 'nennt die Datei'},
    'same time': {
        'en': 'same time',
        'de': 'zur selben Zeit'},
    'This sandbox-ai has no canary log yet; update it with: sandbox-ai update': {
        'en': 'This sandbox-ai has no canary log yet; update it with: sandbox-ai update',
        'de': 'Dieses sandbox-ai hat noch kein Canary-Log; aktualisieren Sie es mit: sandbox-ai update'},
    'Reading the canary log…': {
        'en': 'Reading the canary log…',
        'de': 'Lese das Canary-Log…'},
    'Cannot read the canary log: {error}': {
        'en': 'Cannot read the canary log: {error}',
        'de': 'Das Canary-Log lässt sich nicht lesen: {error}'},
    "Nothing of the agent's in that time window.": {
        'en': "Nothing of the agent's in that time window.",
        'de': 'Nichts vom Agenten in diesem Zeitfenster.'},
    'No activity timeline for this run: nothing to link.': {
        'en': 'No activity timeline for this run: nothing to link.',
        'de': 'Kein Aktivitätsverlauf für diesen Lauf: nichts zu verknüpfen.'},
    '{count} canary event(s) in {runs} run(s)': {
        'en': '{count} canary event(s) in {runs} run(s)',
        'de': '{count} Canary-Ereignis(se) in {runs} Lauf/Läufen'},
    'No canary was touched: no decoy file opened, no token sent.': {
        'en': 'No canary was touched: no decoy file opened, no token sent.',
        'de': 'Kein Canary berührt: keine Köderdatei geöffnet, kein Token gesendet.'},
    'Timeline': {
        'en': 'Timeline',
        'de': 'Verlauf'},
    'Canaries': {
        'en': 'Canaries',
        'de': 'Canaries'},
    'Canaries ({count})': {
        'en': 'Canaries ({count})',
        'de': 'Canaries ({count})'},
    # <<< canary log <<<
    'ct101': {
        'en': 'ct101',
        'de': 'ct101'},
    'user@192.0.2.10': {
        'en': 'user@192.0.2.10',
        'de': 'benutzer@192.0.2.10'},
    'Add a host': {
        'en': 'Add a host',
        'de': 'Host hinzufügen'},
    'Asking {host}…': {
        'en': 'Asking {host}…',
        'de': 'Frage {host}…'},
    'Defaults': {
        'en': 'Defaults',
        'de': 'Standards'},
    'Doctor': {
        'en': 'Doctor',
        'de': 'Doctor'},
    'For every start on {host}, in {path}': {
        'en': 'For every start on {host}, in {path}',
        'de': 'Für jeden Start auf {host}, in {path}'},
    'Key': {
        'en': 'Key',
        'de': 'Schlüssel'},
    'Other machines whose sandbox-ai this app manages over SSH (the web interface, sandbox-ai web, uses the same list).': {
        'en': 'Other machines whose sandbox-ai this app manages over SSH (the web interface, sandbox-ai web, uses the same list).',
        'de': 'Andere Maschinen, deren sandbox-ai diese App über SSH verwaltet (die Weboberfläche, sandbox-ai web, nutzt dieselbe Liste).'},
    'Port': {
        'en': 'Port',
        'de': 'Port'},
    'SSH': {
        'en': 'SSH',
        'de': 'SSH'},
    'Test': {
        'en': 'Test',
        'de': 'Testen'},
    'Testing {host}…': {
        'en': 'Testing {host}…',
        'de': 'Teste {host}…'},
    'This sandbox-ai has no hosts yet: update it.': {
        'en': 'This sandbox-ai has no hosts yet: update it.',
        'de': 'Dieses sandbox-ai kennt noch keine Hosts: bitte aktualisieren.'},
    'Your user needs SSH access with a key (ssh-copy-id), and sandbox-ai installed there. The host key is learnt at the first contact and checked from then on.': {
        'en': 'Your user needs SSH access with a key (ssh-copy-id), and sandbox-ai installed there. The host key is learnt at the first contact and checked from then on.',
        'de': 'Dein Benutzer braucht dort SSH-Zugang mit Schlüssel (ssh-copy-id) und ein installiertes sandbox-ai. Der Host-Schlüssel wird beim ersten Kontakt gelernt und ab dann geprüft.'},
    '{name} (this machine)': {
        'en': '{name} (this machine)',
        'de': '{name} (diese Maschine)'},
    '~/.ssh/id_ed25519 (optional)': {
        'en': '~/.ssh/id_ed25519 (optional)',
        'de': '~/.ssh/id_ed25519 (optional)'},
    'Privacy': {
        'en': 'Privacy',
        'de': 'Datenschutz'},
    'off: no usage statistics, error reports, feedback or update checks (recommended)': {
        'en': 'off: no usage statistics, error reports, feedback or update checks (recommended)',
        'de': 'aus: keine Nutzungsstatistik, Fehlerberichte, Feedback oder Update-Prüfung (empfohlen)'},
    "strict: also no other calls than the model's": {
        'en': "strict: also no other calls than the model's",
        'de': 'streng: auch keine anderen Aufrufe als die des Modells'},
    "on: the agents' own settings": {
        'en': "on: the agents' own settings",
        'de': 'an: die eigenen Einstellungen der Agenten'},
    'Telemetry of Claude Code, Codex and OpenCode': {
        'en': 'Telemetry of Claude Code, Codex and OpenCode',
        'de': 'Telemetrie von Claude Code, Codex und OpenCode'},
    'This sandbox-ai has no telemetry setting yet: update it.': {
        'en': 'This sandbox-ai has no telemetry setting yet: update it.',
        'de': 'Dieses sandbox-ai hat noch keine Telemetrie-Einstellung: bitte aktualisieren.'},
    'For every start, in {path}; --telemetry on the command line wins, the security modes high and paranoid allow no less than off and strict.': {
        'en': 'For every start, in {path}; --telemetry on the command line wins, the security modes high and paranoid allow no less than off and strict.',
        'de': 'Für jeden Start, in {path}; --telemetry auf der Befehlszeile gewinnt, die Sicherheitsmodi high und paranoid lassen nicht weniger als off und strict zu.'},
    'Refused': {
        'en': 'Refused',
        'de': 'Abgelehnt'},
    'Download xterm.js now': {
        'en': 'Download xterm.js now',
        'de': 'xterm.js jetzt herunterladen'},
    'Runs scripts/vendor_xterm.py: the pinned xterm.js from registry.npmjs.org, checked before it is used': {
        'en': 'Runs scripts/vendor_xterm.py: the pinned xterm.js from registry.npmjs.org, checked before it is used',
        'de': 'Führt scripts/vendor_xterm.py aus: das gepinnte xterm.js von registry.npmjs.org, vor der Verwendung geprüft'},
    'Terminals inside the app are not available: {why}. Commands open in a terminal window instead. Install QtWebEngine with: sandbox-ai-update --force (or pip install PySide6-Addons).': {
        'en': 'Terminals inside the app are not available: {why}. Commands open in a terminal window instead. Install QtWebEngine with: sandbox-ai-update --force (or pip install PySide6-Addons).',
        'de': 'Terminals in der App sind nicht verfügbar: {why}. Befehle öffnen sich stattdessen in einem Terminalfenster. QtWebEngine installieren mit: sandbox-ai-update --force (oder pip install PySide6-Addons).'},
    'Download failed: {error}': {
        'en': 'Download failed: {error}',
        'de': 'Herunterladen fehlgeschlagen: {error}'},
    'Downloading xterm.js…': {
        'en': 'Downloading xterm.js…',
        'de': 'Lade xterm.js herunter …'},
    'exit status {status}': {
        'en': 'exit status {status}',
        'de': 'Exit-Status {status}'},
    'Running sandbox {name}: workspace': {
        'en': 'Running sandbox {name}: workspace',
        'de': 'Laufende Sandbox {name}: Arbeitsordner'},
    'Running sandbox {name}: HOME': {
        'en': 'Running sandbox {name}: HOME',
        'de': 'Laufende Sandbox {name}: HOME'},
    'Session {name}: workspace': {
        'en': 'Session {name}: workspace',
        'de': 'Sitzung {name}: Arbeitsordner'},
    'Session {name}: HOME': {
        'en': 'Session {name}: HOME',
        'de': 'Sitzung {name}: HOME'},
    'Current project': {
        'en': 'Current project',
        'de': 'Aktuelles Projekt'},
    'Chosen folder': {
        'en': 'Chosen folder',
        'de': 'Gewählter Ordner'},
    'connect to {destination}': {
        'en': 'connect to {destination}',
        'de': 'Verbindung zu {destination}'},
    'look up the name {name}': {
        'en': 'look up the name {name}',
        'de': 'den Namen {name} auflösen'},
    'push {refs} to {url} despite privacy warnings': {
        'en': 'push {refs} to {url} despite privacy warnings',
        'de': '{refs} trotz Datenschutz-Warnungen nach {url} pushen'},
    'open {url} in your browser': {
        'en': 'open {url} in your browser',
        'de': '{url} in deinem Browser öffnen'},
    '{requests} HTTP requests, {processes} processes; pushes: {ok} checked, {refused} refused': {
        'en': '{requests} HTTP requests, {processes} processes; pushes: {ok} checked, {refused} refused',
        'de': '{requests} HTTP-Anfragen, {processes} Prozesse; Pushes: {ok} geprüft, {refused} abgelehnt'},
    'once': {
        'en': 'once',
        'de': 'einmal'},
    'for this run': {
        'en': 'for this run',
        'de': 'für diesen Lauf'},
    'always (this project)': {
        'en': 'always (this project)',
        'de': 'immer (dieses Projekt)'},
    'Allow': {
        'en': 'Allow',
        'de': 'Erlauben'},
    'Deny': {
        'en': 'Deny',
        'de': 'Ablehnen'},
    'History': {
        'en': 'History',
        'de': 'Verlauf'},
    'Other folder…': {
        'en': 'Other folder…',
        'de': 'Anderer Ordner…'},
    'Refresh': {
        'en': 'Refresh',
        'de': 'Aktualisieren'},
    'Log folder': {
        'en': 'Log folder',
        'de': 'Log-Ordner'},
    'Up': {
        'en': 'Up',
        'de': 'Nach oben'},
    'Copy to the host…': {
        'en': 'Copy to the host…',
        'de': 'Auf den Host kopieren…'},
    'Copy files here…': {
        'en': 'Copy files here…',
        'de': 'Dateien hierher kopieren…'},
    'Copy a folder here…': {
        'en': 'Copy a folder here…',
        'de': 'Einen Ordner hierher kopieren…'},
    'Replace existing files': {
        'en': 'Replace existing files',
        'de': 'Vorhandene Dateien ersetzen'},
    'Symbolic links are shown but never followed or copied; copies stop at {limit} MiB.': {
        'en': 'Symbolic links are shown but never followed or copied; copies stop at {limit} MiB.',
        'de': 'Symbolische Links werden angezeigt, aber nie verfolgt oder kopiert; Kopien enden bei {limit} MiB.'},
    'Folder to browse': {
        'en': 'Folder to browse',
        'de': 'Ordner zum Durchsuchen'},
    'folder': {
        'en': 'folder',
        'de': 'Ordner'},
    'file': {
        'en': 'file',
        'de': 'Datei'},
    'symbolic link': {
        'en': 'symbolic link',
        'de': 'symbolischer Link'},
    'special file': {
        'en': 'special file',
        'de': 'Spezialdatei'},
    'Copy to which folder on the host?': {
        'en': 'Copy to which folder on the host?',
        'de': 'In welchen Ordner auf dem Host kopieren?'},
    'Files to copy into {path}': {
        'en': 'Files to copy into {path}',
        'de': 'Dateien, die nach {path} kopiert werden'},
    'Folder to copy into {path}': {
        'en': 'Folder to copy into {path}',
        'de': 'Ordner, der nach {path} kopiert wird'},
    'Build all': {
        'en': 'Build all',
        'de': 'Alle bauen'},
    'filter (name or version)': {
        'en': 'filter (name or version)',
        'de': 'Filter (Name oder Version)'},
    '{count} of {total} images built': {
        'en': '{count} of {total} images built',
        'de': '{count} von {total} Images gebaut'},
    '{count} packages in {agent}': {
        'en': '{count} packages in {agent}',
        'de': '{count} Pakete in {agent}'},
    'Agent logins for sandboxes': {
        'en': 'Agent logins for sandboxes',
        'de': 'Agent-Anmeldungen für Sandboxen'},
    'Log in…': {
        'en': 'Log in…',
        'de': 'Anmelden…'},
    "Runs the agent's own login inside a sandbox and keeps it for all sandboxes": {
        'en': "Runs the agent's own login inside a sandbox and keeps it for all sandboxes",
        'de': 'Führt die Anmeldung des Agenten in einer Sandbox aus und behält sie für alle Sandboxen'},
    'Log out…': {
        'en': 'Log out…',
        'de': 'Abmelden…'},
    'Secrets (API keys and tokens for --secret-env and MCP servers)': {
        'en': 'Secrets (API keys and tokens for --secret-env and MCP servers)',
        'de': 'Geheimnisse (API-Schlüssel und Tokens für --secret-env und MCP-Server)'},
    'the value (never shown, passed on standard input)': {
        'en': 'the value (never shown, passed on standard input)',
        'de': 'der Wert (nie angezeigt, über die Standardeingabe übergeben)'},
    'Name': {
        'en': 'Name',
        'de': 'Name'},
    'Value': {
        'en': 'Value',
        'de': 'Wert'},
    'Keep secret': {
        'en': 'Keep secret',
        'de': 'Geheimnis speichern'},
    'Delete…': {
        'en': 'Delete…',
        'de': 'Löschen…'},
    'logged in': {
        'en': 'logged in',
        'de': 'angemeldet'},
    'no login': {
        'en': 'no login',
        'de': 'keine Anmeldung'},
    'expired, log in again': {
        'en': 'expired, log in again',
        'de': 'abgelaufen, neu anmelden'},
    'cannot be read': {
        'en': 'cannot be read',
        'de': 'nicht lesbar'},
    'Kept in: {store}': {
        'en': 'Kept in: {store}',
        'de': 'Gespeichert in: {store}'},
    'connection': {
        'en': 'connection',
        'de': 'Verbindung'},
    'name lookup': {
        'en': 'name lookup',
        'de': 'Namensauflösung'},
    'push': {
        'en': 'push',
        'de': 'Push'},
    'open a URL': {
        'en': 'open a URL',
        'de': 'URL öffnen'},
    'MCP tool call': {
        'en': 'MCP tool call',
        'de': 'MCP-Werkzeugaufruf'},
    'HTTP request': {
        'en': 'HTTP request',
        'de': 'HTTP-Anfrage'},
    'agent tool': {
        'en': 'agent tool',
        'de': 'Werkzeug des Agenten'},
    'device': {
        'en': 'device',
        'de': 'Gerät'},
    'canary': {
        'en': 'canary',
        'de': 'Köder'},
    'rule {rule} of {ruleset}': {
        'en': 'rule {rule} of {ruleset}',
        'de': 'Regel {rule} aus {ruleset}'},
    'call the MCP tool {tool} of {server} with {arguments} ({rule})': {
        'en': 'call the MCP tool {tool} of {server} with {arguments} ({rule})',
        'de': 'das MCP-Werkzeug {tool} von {server} mit {arguments} aufrufen ({rule})'},
    'let {agent} use its tool {tool} with {arguments} ({rule})': {
        'en': 'let {agent} use its tool {tool} with {arguments} ({rule})',
        'de': '{agent} sein Werkzeug {tool} mit {arguments} benutzen lassen ({rule})'},
    'attach the device {device} ({rule})': {
        'en': 'attach the device {device} ({rule})',
        'de': 'das Gerät {device} anschließen ({rule})'},
    'canary {name}: {summary}': {
        'en': 'canary {name}: {summary}',
        'de': 'Köder {name}: {summary}'},
    '(named {host} by the agent, not verified)': {
        'en': '(named {host} by the agent, not verified)',
        'de': '(vom Agenten {host} genannt, nicht geprüft)'},
    'Run': {
        'en': 'Run',
        'de': 'Lauf'},
    'Kind': {
        'en': 'Kind',
        'de': 'Art'},
    'Question': {
        'en': 'Question',
        'de': 'Frage'},
    'Asked': {
        'en': 'Asked',
        'de': 'Gefragt'},
    'Refused in': {
        'en': 'Refused in',
        'de': 'Abgelehnt in'},
    'Scope': {
        'en': 'Scope',
        'de': 'Geltung'},
    'Time': {
        'en': 'Time',
        'de': 'Zeit'},
    'Answer': {
        'en': 'Answer',
        'de': 'Antwort'},
    'Reason': {
        'en': 'Reason',
        'de': 'Grund'},
    'Open in your browser: {url}': {
        'en': 'Open in your browser: {url}',
        'de': 'In deinem Browser öffnen: {url}'},
    'Not answered': {
        'en': 'Not answered',
        'de': 'Nicht beantwortet'},
    'Logs': {
        'en': 'Logs',
        'de': 'Logs'},
    'Model calls': {
        'en': 'Model calls',
        'de': 'Modellaufrufe'},
    'Tokens': {
        'en': 'Tokens',
        'de': 'Tokens'},
    'HTTP requests per host': {
        'en': 'HTTP requests per host',
        'de': 'HTTP-Anfragen pro Host'},
    'Credentials sent (header names only)': {
        'en': 'Credentials sent (header names only)',
        'de': 'Gesendete Zugangsdaten (nur Header-Namen)'},
    'Refused connections': {
        'en': 'Refused connections',
        'de': 'Abgelehnte Verbindungen'},
    'Programs started': {
        'en': 'Programs started',
        'de': 'Gestartete Programme'},
    'MCP tool calls (all servers)': {
        'en': 'MCP tool calls (all servers)',
        'de': 'MCP-Werkzeugaufrufe (alle Server)'},
    'No logs yet: they appear once a sandbox has run with its connection proxy.': {
        'en': 'No logs yet: they appear once a sandbox has run with its connection proxy.',
        'de': 'Noch keine Logs: Sie erscheinen, sobald eine Sandbox mit ihrem Verbindungs-Proxy gelaufen ist.'},
    '(only the newest entries: {count} older lines not read)': {
        'en': '(only the newest entries: {count} older lines not read)',
        'de': '(nur die neuesten Einträge: {count} ältere Zeilen nicht gelesen)'},
    'Folder': {
        'en': 'Folder',
        'de': 'Ordner'},
    'Type': {
        'en': 'Type',
        'de': 'Typ'},
    'Size': {
        'en': 'Size',
        'de': 'Größe'},
    'Modified': {
        'en': 'Modified',
        'de': 'Geändert'},
    'Points to (not followed)': {
        'en': 'Points to (not followed)',
        'de': 'Zeigt auf (nicht verfolgt)'},
    '{files} file(s) in {folders} folder(s), {size} copied': {
        'en': '{files} file(s) in {folders} folder(s), {size} copied',
        'de': '{files} Datei(en) in {folders} Ordner(n), {size} kopiert'},
    'skipped: {list}': {
        'en': 'skipped: {list}',
        'de': 'übersprungen: {list}'},
    'Files': {
        'en': 'Files',
        'de': 'Dateien'},
    'Select files or folders first.': {
        'en': 'Select files or folders first.',
        'de': 'Wähle zuerst Dateien oder Ordner.'},
    'Agent': {
        'en': 'Agent',
        'de': 'Agent'},
    'Built': {
        'en': 'Built',
        'de': 'Gebaut'},
    'Agent version': {
        'en': 'Agent version',
        'de': 'Agent-Version'},
    'Created': {
        'en': 'Created',
        'de': 'Erstellt'},
    'Image': {
        'en': 'Image',
        'de': 'Image'},
    'Version': {
        'en': 'Version',
        'de': 'Version'},
    'Asking Podman…': {
        'en': 'Asking Podman…',
        'de': 'Frage Podman…'},
    'Cannot list the images: {error}': {
        'en': 'Cannot list the images: {error}',
        'de': 'Die Images können nicht aufgelistet werden: {error}'},
    'Images': {
        'en': 'Images',
        'de': 'Images'},
    'Select an image first.': {
        'en': 'Select an image first.',
        'de': 'Wähle zuerst ein Image.'},
    'The {agent} image is not built.': {
        'en': 'The {agent} image is not built.',
        'de': 'Das Image von {agent} ist nicht gebaut.'},
    'Reading the packages of {agent}…': {
        'en': 'Reading the packages of {agent}…',
        'de': 'Lese die Pakete von {agent}…'},
    'Cannot read the packages: {error}': {
        'en': 'Cannot read the packages: {error}',
        'de': 'Die Pakete können nicht gelesen werden: {error}'},
    'Login from': {
        'en': 'Login from',
        'de': 'Anmeldung aus'},
    'State': {
        'en': 'State',
        'de': 'Zustand'},
    'File': {
        'en': 'File',
        'de': 'Datei'},
    'Kept in': {
        'en': 'Kept in',
        'de': 'Gespeichert in'},
    'Logins': {
        'en': 'Logins',
        'de': 'Anmeldungen'},
    'Select an agent first.': {
        'en': 'Select an agent first.',
        'de': 'Wähle zuerst einen Agenten.'},
    'Secret': {
        'en': 'Secret',
        'de': 'Geheimnis'},
    'A name is letters, digits and _ (up to 64), not starting with a digit.': {
        'en': 'A name is letters, digits and _ (up to 64), not starting with a digit.',
        'de': 'Ein Name besteht aus Buchstaben, Ziffern und _ (bis zu 64), beginnt aber nicht mit einer Ziffer.'},
    'Enter the value first.': {
        'en': 'Enter the value first.',
        'de': 'Gib zuerst den Wert ein.'},
    'Not kept: {error}': {
        'en': 'Not kept: {error}',
        'de': 'Nicht gespeichert: {error}'},
    'Select a secret first.': {
        'en': 'Select a secret first.',
        'de': 'Wähle zuerst ein Geheimnis.'},
    'Delete secret': {
        'en': 'Delete secret',
        'de': 'Geheimnis löschen'},
    'Delete the secret {name}?': {
        'en': 'Delete the secret {name}?',
        'de': 'Das Geheimnis {name} löschen?'},
    'Cannot read the questions: {error}': {
        'en': 'Cannot read the questions: {error}',
        'de': 'Die Fragen können nicht gelesen werden: {error}'},
    '{seconds} s ago': {
        'en': '{seconds} s ago',
        'de': 'vor {seconds} s'},
    'no time limit': {
        'en': 'no time limit',
        'de': 'ohne Zeitlimit'},
    'sandbox-ai refused the answer.': {
        'en': 'sandbox-ai refused the answer.',
        'de': 'sandbox-ai hat die Antwort abgelehnt.'},
    'Model': {
        'en': 'Model',
        'de': 'Modell'},
    'Calls': {
        'en': 'Calls',
        'de': 'Aufrufe'},
    'Host': {
        'en': 'Host',
        'de': 'Host'},
    'Requests': {
        'en': 'Requests',
        'de': 'Anfragen'},
    'Header': {
        'en': 'Header',
        'de': 'Header'},
    'Times': {
        'en': 'Times',
        'de': 'Anzahl'},
    'What': {
        'en': 'What',
        'de': 'Was'},
    'Program': {
        'en': 'Program',
        'de': 'Programm'},
    'Server': {
        'en': 'Server',
        'de': 'Server'},
    'Tool': {
        'en': 'Tool',
        'de': 'Werkzeug'},
    'Cannot read the logs: {error}': {
        'en': 'Cannot read the logs: {error}',
        'de': 'Die Logs können nicht gelesen werden: {error}'},
    '{name} is a symbolic link to {target}; it is not followed.': {
        'en': '{name} is a symbolic link to {target}; it is not followed.',
        'de': '{name} ist ein symbolischer Link auf {target}; er wird nicht verfolgt.'},
    'Log out': {
        'en': 'Log out',
        'de': 'Abmelden'},
    "Forget the stored {agent} login? Sandboxes then use the host's login again.": {
        'en': "Forget the stored {agent} login? Sandboxes then use the host's login again.",
        'de': 'Die gespeicherte Anmeldung von {agent} vergessen? Sandboxen verwenden dann wieder die Anmeldung des Hosts.'},
    '{seconds} s': {
        'en': '{seconds} s',
        'de': '{seconds} s'},
    'refused': {
        'en': 'refused',
        'de': 'abgelehnt'},
    'open': {
        'en': 'open',
        'de': 'offen'},
    'Cannot open {path}: {error}': {
        'en': 'Cannot open {path}: {error}',
        'de': '{path} kann nicht geöffnet werden: {error}'},
    'yes': {
        'en': 'yes',
        'de': 'ja'},
    'not built': {
        'en': 'not built',
        'de': 'nicht gebaut'},
    'sandbox-ai login store': {
        'en': 'sandbox-ai login store',
        'de': 'Anmeldespeicher von sandbox-ai'},
    "the host's login": {
        'en': "the host's login",
        'de': 'Anmeldung des Hosts'},
    'allowed': {
        'en': 'allowed',
        'de': 'erlaubt'},
    'denied': {
        'en': 'denied',
        'de': 'abgelehnt'},
    'your system keyring (Secret Service)': {
        'en': 'your system keyring (Secret Service)',
        'de': 'dein System-Schlüsselbund (Secret Service)'},
    'private files (mode 0600)': {
        'en': 'private files (mode 0600)',
        'de': 'private Dateien (Modus 0600)'},
    'switched off by SANDBOX_AI_APP_NO_WEBENGINE': {
        'en': 'switched off by SANDBOX_AI_APP_NO_WEBENGINE',
        'de': 'durch SANDBOX_AI_APP_NO_WEBENGINE abgeschaltet'},
    'QtWebEngine is not installed ({error})': {
        'en': 'QtWebEngine is not installed ({error})',
        'de': 'QtWebEngine ist nicht installiert ({error})'},
    'xterm.js is not vendored ({files}); run scripts/vendor_xterm.py': {
        'en': 'xterm.js is not vendored ({files}); run scripts/vendor_xterm.py',
        'de': 'xterm.js ist nicht eingebunden ({files}); führe scripts/vendor_xterm.py aus'},
    '[sandbox-ai: exit status {status}; close the tab with ×]': {
        'en': '[sandbox-ai: exit status {status}; close the tab with ×]',
        'de': '[sandbox-ai: Exit-Status {status}; schließe den Tab mit ×]'},
    'Every terminal of the app opens here as a tab: starts in the app, sessions you attach, builds and logins. Any start that runs in tmux, wherever it was started, can be attached from the list.': {
        'en': 'Every terminal of the app opens here as a tab: starts in the app, sessions you attach, builds and logins. Any start that runs in tmux, wherever it was started, can be attached from the list.',
        'de': 'Jedes Terminal der App öffnet sich hier als Tab: Starts in der App, Sitzungen, mit denen Sie sich verbinden, Builds und Logins. Mit jedem Start, der in tmux läuft, gleich wo er gestartet wurde, können Sie sich über die Liste verbinden.'},
    'Runs in the foreground of its own terminal (started with --no-tmux, as a task or without a terminal): nothing to attach. Start it with tmux (the default for interactive starts) to attach from here.': {
        'en': 'Runs in the foreground of its own terminal (started with --no-tmux, as a task or without a terminal): nothing to attach. Start it with tmux (the default for interactive starts) to attach from here.',
        'de': 'Läuft im Vordergrund seines eigenen Terminals (gestartet mit --no-tmux, als Aufgabe oder ohne Terminal): nichts zum Verbinden. Starten Sie ihn mit tmux (Standard für interaktive Starts), um sich von hier aus zu verbinden.'},
    'Paused: resume it to attach.': {
        'en': 'Paused: resume it to attach.',
        'de': 'Pausiert: Setzen Sie ihn fort, um sich zu verbinden.'},
    'An MCP server sandbox: it has no terminal of its own.': {
        'en': 'An MCP server sandbox: it has no terminal of its own.',
        'de': 'Eine Sandbox eines MCP-Servers: Sie hat kein eigenes Terminal.'},
    'Damaged: see sandbox-ai list.': {
        'en': 'Damaged: see sandbox-ai list.',
        'de': 'Beschädigt: siehe sandbox-ai list.'},
    'Running starts': {
        'en': 'Running starts',
        'de': 'Laufende Starts'},
    'This machine, or a host of sandbox-ai hosts (over SSH)': {
        'en': 'This machine, or a host of sandbox-ai hosts (over SSH)',
        'de': 'Dieser Rechner oder ein Host von sandbox-ai hosts (über SSH)'},
    'Terminal': {
        'en': 'Terminal',
        'de': 'Terminal'},
    'not a list': {
        'en': 'not a list',
        'de': 'keine Liste'},
    '{count} running on {host}': {
        'en': '{count} running on {host}',
        'de': '{count} laufen auf {host}'},
    'could not be read: {problem}': {
        'en': 'could not be read: {problem}',
        'de': 'nicht lesbar: {problem}'},
    'Show its tab': {
        'en': 'Show its tab',
        'de': 'Seinen Tab zeigen'},
    'Open terminal': {
        'en': 'Open terminal',
        'de': 'Terminal öffnen'},
    'Attach to {name} in a terminal tab (detach with Ctrl-b d: it keeps running)': {
        'en': 'Attach to {name} in a terminal tab (detach with Ctrl-b d: it keeps running)',
        'de': 'Mit {name} in einem Terminal-Tab verbinden (lösen mit Strg-b d: er läuft weiter)'},
    'Terminals inside the app are not available: {why}. Commands open in a terminal window instead.': {
        'en': 'Terminals inside the app are not available: {why}. Commands open in a terminal window instead.',
        'de': 'Terminals in der App sind nicht verfügbar: {why}. Befehle öffnen sich stattdessen in einem Terminalfenster.'},
    '{title} (ended)': {
        'en': '{title} (ended)',
        'de': '{title} (beendet)'},
    'Close terminal': {
        'en': 'Close terminal',
        'de': 'Terminal schließen'},
    'The command in this tab still runs. End it?': {
        'en': 'The command in this tab still runs. End it?',
        'de': 'Der Befehl in diesem Tab läuft noch. Beenden?'},
    'Workspace': {
        'en': 'Workspace',
        'de': 'Arbeitsordner'},
    'Network': {
        'en': 'Network',
        'de': 'Netzwerk'},
    'Proxy & privacy': {
        'en': 'Proxy & privacy',
        'de': 'Proxy & Datenschutz'},
    'Git': {
        'en': 'Git',
        'de': 'Git'},
    'Login': {
        'en': 'Login',
        'de': 'Anmeldung'},
    'Display & terminal': {
        'en': 'Display & terminal',
        'de': 'Anzeige & Terminal'},
    'Hardware': {
        'en': 'Hardware',
        'de': 'Hardware'},
    'Limits': {
        'en': 'Limits',
        'de': 'Grenzen'},
    'Engine': {
        'en': 'Engine',
        'de': 'Engine'},
    'More': {
        'en': 'More',
        'de': 'Weitere'},
    'Attach': {
        'en': 'Attach',
        'de': 'Verbinden'},
    'Attach in the app': {
        'en': 'Attach in the app',
        'de': 'In der App verbinden'},
    'Pause': {
        'en': 'Pause',
        'de': 'Pausieren'},
    'Resume': {
        'en': 'Resume',
        'de': 'Fortsetzen'},
    'Stop': {
        'en': 'Stop',
        'de': 'Stoppen'},
    'Delete': {
        'en': 'Delete',
        'de': 'Löschen'},
    'Start': {
        'en': 'Start',
        'de': 'Start'},
    'Sandboxes': {
        'en': 'Sandboxes',
        'de': 'Sandboxen'},
    'Approvals': {
        'en': 'Approvals',
        'de': 'Freigaben'},
    'Connections': {
        'en': 'Connections',
        'de': 'Verbindungen'},
    'Statistics': {
        'en': 'Statistics',
        'de': 'Statistik'},
    'MCP servers': {
        'en': 'MCP servers',
        'de': 'MCP-Server'},
    'Credentials': {
        'en': 'Credentials',
        'de': 'Zugangsdaten'},
    'Terminals': {
        'en': 'Terminals',
        'de': 'Terminals'},
    'System': {
        'en': 'System',
        'de': 'System'},
    'Choose a folder': {
        'en': 'Choose a folder',
        'de': 'Ordner wählen'},
    'Load': {
        'en': 'Load',
        'de': 'Laden'},
    'Save as profile…': {
        'en': 'Save as profile…',
        'de': 'Als Profil speichern…'},
    'Open project folder…': {
        'en': 'Open project folder…',
        'de': 'Projektordner öffnen…'},
    "Review a folder's .sandbox-ai.json, trust it if you agree, and start in that folder": {
        'en': "Review a folder's .sandbox-ai.json, trust it if you agree, and start in that folder",
        'de': 'Die .sandbox-ai.json eines Ordners prüfen, ihr bei Zustimmung vertrauen und in diesem Ordner starten'},
    'arguments for the agent itself, e.g. --model sonnet': {
        'en': 'arguments for the agent itself, e.g. --model sonnet',
        'de': 'Argumente für den Agenten selbst, z. B. --model sonnet'},
    'Dry run': {
        'en': 'Dry run',
        'de': 'Probelauf'},
    'Check the options and show the plan; nothing is started': {
        'en': 'Check the options and show the plan; nothing is started',
        'de': 'Optionen prüfen und den Plan zeigen; nichts wird gestartet'},
    'Start in the app': {
        'en': 'Start in the app',
        'de': 'In der App starten'},
    'In a terminal tab of this window (Terminals)': {
        'en': 'In a terminal tab of this window (Terminals)',
        'de': 'In einem Terminal-Tab dieses Fensters (Terminals)'},
    'Start in a terminal window': {
        'en': 'Start in a terminal window',
        'de': 'In einem Terminalfenster starten'},
    'session name': {
        'en': 'session name',
        'de': 'Sitzungsname'},
    'Start as session': {
        'en': 'Start as session',
        'de': 'Als Sitzung starten'},
    'A detachable session: attach and detach from the Sandboxes page': {
        'en': 'A detachable session: attach and detach from the Sandboxes page',
        'de': 'Eine abtrennbare Sitzung: auf der Seite Sandboxen verbinden und trennen'},
    'Save as project configuration…': {
        'en': 'Save as project configuration…',
        'de': 'Als Projektkonfiguration speichern…'},
    'Write .sandbox-ai.json into the first workspace and trust it': {
        'en': 'Write .sandbox-ai.json into the first workspace and trust it',
        'de': '.sandbox-ai.json in den ersten Arbeitsordner schreiben und ihr vertrauen'},
    'Save profile': {
        'en': 'Save profile',
        'de': 'Profil speichern'},
    'Name of the profile:': {
        'en': 'Name of the profile:',
        'de': 'Name des Profils:'},
    'Project folder with .sandbox-ai.json': {
        'en': 'Project folder with .sandbox-ai.json',
        'de': 'Projektordner mit .sandbox-ai.json'},
    'Plan (dry run)': {
        'en': 'Plan (dry run)',
        'de': 'Plan (Probelauf)'},
    'Write and trust': {
        'en': 'Write and trust',
        'de': 'Schreiben und vertrauen'},
    'Host names (TCP/443)': {
        'en': 'Host names (TCP/443)',
        'de': 'Hostnamen (TCP/443)'},
    'IP:PORT (TCP)': {
        'en': 'IP:PORT (TCP)',
        'de': 'IP:PORT (TCP)'},
    'Addresses or networks (every port)': {
        'en': 'Addresses or networks (every port)',
        'de': 'Adressen oder Netze (jeder Port)'},
    'Sessions': {
        'en': 'Sessions',
        'de': 'Sitzungen'},
    'Allow for {run}': {
        'en': 'Allow for {run}',
        'de': 'Erlauben für {run}'},
    'Revoke for {run}': {
        'en': 'Revoke for {run}',
        'de': 'Zurücknehmen für {run}'},
    'filter (host, URL, event)': {
        'en': 'filter (host, URL, event)',
        'de': 'Filter (Host, URL, Ereignis)'},
    'only refused and errors': {
        'en': 'only refused and errors',
        'de': 'nur Abgelehntes und Fehler'},
    'follow': {
        'en': 'follow',
        'de': 'mitlaufen'},
    'Add {name}': {
        'en': 'Add {name}',
        'de': '{name} hinzufügen'},
    'Name for --mcp': {
        'en': 'Name for --mcp',
        'de': 'Name für --mcp'},
    'Memory': {
        'en': 'Memory',
        'de': 'Speicher'},
    'Processes': {
        'en': 'Processes',
        'de': 'Prozesse'},
    'Your MCP servers (use them with --mcp NAME)': {
        'en': 'Your MCP servers (use them with --mcp NAME)',
        'de': 'Deine MCP-Server (verwende sie mit --mcp NAME)'},
    'Catalogs: sandbox-ai-mcp (mcp:) and axross-mcp-hub (hub:)': {
        'en': 'Catalogs: sandbox-ai-mcp (mcp:) and axross-mcp-hub (hub:)',
        'de': 'Kataloge: sandbox-ai-mcp (mcp:) und axross-mcp-hub (hub:)'},
    'Add…': {
        'en': 'Add…',
        'de': 'Hinzufügen…'},
    'sandbox-ai': {
        'en': 'sandbox-ai',
        'de': 'sandbox-ai'},
    'Version {version}, from {path}': {
        'en': 'Version {version}, from {path}',
        'de': 'Version {version}, aus {path}'},
    'Update check not run yet.': {
        'en': 'Update check not run yet.',
        'de': 'Noch nicht auf Updates geprüft.'},
    'Check for updates': {
        'en': 'Check for updates',
        'de': 'Auf Updates prüfen'},
    'Host check (sandbox-ai doctor)': {
        'en': 'Host check (sandbox-ai doctor)',
        'de': 'Host-Prüfung (sandbox-ai doctor)'},
    'Check': {
        'en': 'Check',
        'de': 'Prüfen'},
    'Images, updates, privacy': {
        'en': 'Images, updates, privacy',
        'de': 'Images, Updates, Datenschutz'},
    'Privacy check of a repository…': {
        'en': 'Privacy check of a repository…',
        'de': 'Datenschutzprüfung eines Repositorys…'},
    'Settings of the app': {
        'en': 'Settings of the app',
        'de': 'Einstellungen der App'},
    'Theme': {
        'en': 'Theme',
        'de': 'Farbschema'},
    'Language': {
        'en': 'Language',
        'de': 'Sprache'},
    'Commands such as builds and logins run': {
        'en': 'Commands such as builds and logins run',
        'de': 'Befehle wie Builds und Anmeldungen laufen'},
    'The language changes when the app is started again.': {
        'en': 'The language changes when the app is started again.',
        'de': 'Die Sprache ändert sich beim nächsten Start der App.'},
    'Git repository': {
        'en': 'Git repository',
        'de': 'Git-Repository'},
    'sandbox-ai {version} from {path}': {
        'en': 'sandbox-ai {version} from {path}',
        'de': 'sandbox-ai {version} aus {path}'},
    'sandbox-ai {available} is available (installed: {installed}); update with: sandbox-ai update': {
        'en': 'sandbox-ai {available} is available (installed: {installed}); update with: sandbox-ai update',
        'de': 'sandbox-ai {available} ist verfügbar (installiert: {installed}); aktualisieren mit: sandbox-ai update'},
    'sandbox-ai {installed} is up to date.': {
        'en': 'sandbox-ai {installed} is up to date.',
        'de': 'sandbox-ai {installed} ist aktuell.'},
    'Add a folder': {
        'en': 'Add a folder',
        'de': 'Einen Ordner hinzufügen'},
    'Profile': {
        'en': 'Profile',
        'de': 'Profil'},
    'Agent arguments': {
        'en': 'Agent arguments',
        'de': 'Agent-Argumente'},
    'Options not shown': {
        'en': 'Options not shown',
        'de': 'Nicht angezeigte Optionen'},
    'The launcher refused these options': {
        'en': 'The launcher refused these options',
        'de': 'Der Launcher hat diese Optionen abgelehnt'},
    'Session': {
        'en': 'Session',
        'de': 'Sitzung'},
    'Give the session a name first.': {
        'en': 'Give the session a name first.',
        'de': 'Gib der Sitzung zuerst einen Namen.'},
    'Project configuration': {
        'en': 'Project configuration',
        'de': 'Projektkonfiguration'},
    'Choose a workspace first: the file is written there.': {
        'en': 'Choose a workspace first: the file is written there.',
        'de': 'Wähle zuerst einen Arbeitsordner: Die Datei wird dort geschrieben.'},
    'Write {path} with this content and trust it?': {
        'en': 'Write {path} with this content and trust it?',
        'de': '{path} mit diesem Inhalt schreiben und ihr vertrauen?'},
    'Written and trusted: {path}': {
        'en': 'Written and trusted: {path}',
        'de': 'Geschrieben und vertraut: {path}'},
    'Proxy': {
        'en': 'Proxy',
        'de': 'Proxy'},
    'Allow…': {
        'en': 'Allow…',
        'de': 'Erlauben…'},
    'Revoke…': {
        'en': 'Revoke…',
        'de': 'Zurücknehmen…'},
    'Allowed during the run': {
        'en': 'Allowed during the run',
        'de': 'Während des Laufs erlaubt'},
    'Connection log': {
        'en': 'Connection log',
        'de': 'Verbindungslog'},
    'Backend': {
        'en': 'Backend',
        'de': 'Backend'},
    'Status': {
        'en': 'Status',
        'de': 'Status'},
    'Select a running sandbox first.': {
        'en': 'Select a running sandbox first.',
        'de': 'Wähle zuerst eine laufende Sandbox.'},
    'Select a session first.': {
        'en': 'Select a session first.',
        'de': 'Wähle zuerst eine Sitzung.'},
    'session {name}': {
        'en': 'session {name}',
        'de': 'Sitzung {name}'},
    'Event': {
        'en': 'Event',
        'de': 'Ereignis'},
    'Method': {
        'en': 'Method',
        'de': 'Methode'},
    'Target': {
        'en': 'Target',
        'de': 'Ziel'},
    'Bytes': {
        'en': 'Bytes',
        'de': 'Bytes'},
    'writable': {
        'en': 'writable',
        'de': 'beschreibbar'},
    'Variables from your environment': {
        'en': 'Variables from your environment',
        'de': 'Variablen aus deiner Umgebung'},
    'Runs': {
        'en': 'Runs',
        'de': 'Führt aus'},
    'Entry': {
        'en': 'Entry',
        'de': 'Eintrag'},
    'Title': {
        'en': 'Title',
        'de': 'Titel'},
    'Published': {
        'en': 'Published',
        'de': 'Veröffentlicht'},
    'Catalogs': {
        'en': 'Catalogs',
        'de': 'Kataloge'},
    'Add': {
        'en': 'Add',
        'de': 'Hinzufügen'},
    'Load the catalogs and select an entry first.': {
        'en': 'Load the catalogs and select an entry first.',
        'de': 'Lade die Kataloge und wähle zuerst einen Eintrag.'},
    'How to fix': {
        'en': 'How to fix',
        'de': 'Abhilfe'},
    'Build images': {
        'en': 'Build images',
        'de': 'Images bauen'},
    'Update sandbox-ai': {
        'en': 'Update sandbox-ai',
        'de': 'sandbox-ai aktualisieren'},
    'as the system': {
        'en': 'as the system',
        'de': 'wie das System'},
    'light': {
        'en': 'light',
        'de': 'hell'},
    'dark': {
        'en': 'dark',
        'de': 'dunkel'},
    'hacker': {
        'en': 'hacker',
        'de': 'Hacker'},
    'cyberpunk': {
        'en': 'cyberpunk',
        'de': 'Cyberpunk'},
    'amber': {
        'en': 'amber',
        'de': 'Bernstein'},
    'unicorn': {
        'en': 'unicorn',
        'de': 'Einhorn'},
    'Windows 3.11': {
        'en': 'Windows 3.11',
        'de': 'Windows 3.11'},
    'Mac': {
        'en': 'Mac',
        'de': 'Mac'},
    'Fallout': {
        'en': 'Fallout',
        'de': 'Fallout'},
    'in a tab of the app (when available)': {
        'en': 'in a tab of the app (when available)',
        'de': 'in einem Tab der App (wenn verfügbar)'},
    'in a terminal window': {
        'en': 'in a terminal window',
        'de': 'in einem Terminalfenster'},
    'Privacy check: {path}': {
        'en': 'Privacy check: {path}',
        'de': 'Datenschutzprüfung: {path}'},
    'sandbox-ai asks: {sandbox}': {
        'en': 'sandbox-ai asks: {sandbox}',
        'de': 'sandbox-ai fragt: {sandbox}'},
    'Sandbox': {
        'en': 'Sandbox',
        'de': 'Sandbox'},
    'Checking for a newer sandbox-ai…': {
        'en': 'Checking for a newer sandbox-ai…',
        'de': 'Suche nach einem neueren sandbox-ai…'},
    'No update information (offline, or not installed with install.sh).': {
        'en': 'No update information (offline, or not installed with install.sh).',
        'de': 'Keine Update-Information (offline oder nicht mit install.sh installiert).'},
    'No terminal': {
        'en': 'No terminal',
        'de': 'Kein Terminal'},
    'Not started': {
        'en': 'Not started',
        'de': 'Nicht gestartet'},
    'Choose a file': {
        'en': 'Choose a file',
        'de': 'Datei wählen'},
    'No project configuration': {
        'en': 'No project configuration',
        'de': 'Keine Projektkonfiguration'},
    'Quit': {
        'en': 'Quit',
        'de': 'Beenden'},
    'These are not options of this sandbox-ai version and were left out:': {
        'en': 'These are not options of this sandbox-ai version and were left out:',
        'de': 'Diese Optionen kennt diese sandbox-ai-Version nicht; sie wurden ausgelassen:'},
    'Not written': {
        'en': 'Not written',
        'de': 'Nicht geschrieben'},
    'on': {
        'en': 'on',
        'de': 'an'},
    'off': {
        'en': 'off',
        'de': 'aus'},
    '(needs {hosts})': {
        'en': '(needs {hosts})',
        'de': '(braucht {hosts})'},
    '(error)': {
        'en': '(error)',
        'de': '(Fehler)'},
    'not yet': {
        'en': 'not yet',
        'de': 'noch nicht'},
    'No terminal emulator found. Set SANDBOX_AI_TERMINAL, or run:': {
        'en': 'No terminal emulator found. Set SANDBOX_AI_TERMINAL, or run:',
        'de': 'Kein Terminalprogramm gefunden. Setze SANDBOX_AI_TERMINAL oder führe aus:'},
    'required': {
        'en': 'required',
        'de': 'nötig'},
    'stopped': {
        'en': 'stopped',
        'de': 'gestoppt'},
    'sandbox-ai not found': {
        'en': 'sandbox-ai not found',
        'de': 'sandbox-ai nicht gefunden'},
    'A path must not contain NUL bytes.': {
        'en': 'A path must not contain NUL bytes.',
        'de': 'Ein Pfad darf keine NUL-Bytes enthalten.'},
    'Cancelled.': {
        'en': 'Cancelled.',
        'de': 'Abgebrochen.'},
    'Choose a file or folder below the folder shown.': {
        'en': 'Choose a file or folder below the folder shown.',
        'de': 'Wählen Sie eine Datei oder einen Ordner unterhalb des angezeigten Ordners.'},
    'Choose a file or folder to copy.': {
        'en': 'Choose a file or folder to copy.',
        'de': 'Wählen Sie eine Datei oder einen Ordner zum Kopieren.'},
    'Folders nested more than {count} deep are not copied.': {
        'en': 'Folders nested more than {count} deep are not copied.',
        'de': 'Ordner, die tiefer als {count} Ebenen verschachtelt sind, werden nicht kopiert.'},
    'More than {count} files and folders; copy a smaller part.': {
        'en': 'More than {count} files and folders; copy a smaller part.',
        'de': 'Mehr als {count} Dateien und Ordner; kopieren Sie einen kleineren Teil.'},
    'More than {size}; copy a smaller part.': {
        'en': 'More than {size}; copy a smaller part.',
        'de': 'Mehr als {size}; kopieren Sie einen kleineren Teil.'},
    'Not a path below the folder: {path}': {
        'en': 'Not a path below the folder: {path}',
        'de': 'Kein Pfad unterhalb des Ordners: {path}'},
    'That folder is no longer below the folder shown (moved while it was opened).': {
        'en': 'That folder is no longer below the folder shown (moved while it was opened).',
        'de': 'Dieser Ordner liegt nicht mehr unterhalb des angezeigten Ordners (beim Öffnen verschoben).'},
    'not a regular file': {
        'en': 'not a regular file',
        'de': 'keine reguläre Datei'},
    '{name} already exists there.': {
        'en': '{name} already exists there.',
        'de': '{name} gibt es dort bereits.'},
    '{name} is a folder there.': {
        'en': '{name} is a folder there.',
        'de': '{name} ist dort ein Ordner.'},
    '{name} is a symbolic link; links are not copied.': {
        'en': '{name} is a symbolic link; links are not copied.',
        'de': '{name} ist ein symbolischer Link; Links werden nicht kopiert.'},
    '{name} is a symbolic link; links are not followed.': {
        'en': '{name} is a symbolic link; links are not followed.',
        'de': '{name} ist ein symbolischer Link; Links wird nicht gefolgt.'},
    '{name} is not a folder (symbolic links are not followed).': {
        'en': '{name} is not a folder (symbolic links are not followed).',
        'de': '{name} ist kein Ordner (symbolischen Links wird nicht gefolgt).'},
    '{name} is not a regular file.': {
        'en': '{name} is not a regular file.',
        'de': '{name} ist keine reguläre Datei.'},
    '“..” is not followed: paths stay below the folder.': {
        'en': '“..” is not followed: paths stay below the folder.',
        'de': '„..“ wird nicht gefolgt: Pfade bleiben unterhalb des Ordners.'},
    'Cancel': {
        'en': 'Cancel',
        'de': 'Abbrechen'},
    'Cancelling…': {
        'en': 'Cancelling…',
        'de': 'Wird abgebrochen…'},
    'Close': {
        'en': 'Close',
        'de': 'Schließen'},
    'Details': {
        'en': 'Details',
        'de': 'Details'},
    'Hide this message': {
        'en': 'Hide this message',
        'de': 'Diese Meldung ausblenden'},
    'OK': {
        'en': 'OK',
        'de': 'OK'},
    'Show or hide what sandbox-ai printed': {
        'en': 'Show or hide what sandbox-ai printed',
        'de': 'Ausgabe von sandbox-ai ein- oder ausblenden'},
    'Stop what runs now': {
        'en': 'Stop what runs now',
        'de': 'Laufende Aktion stoppen'},
    'Sending the answer…': {
        'en': 'Sending the answer…',
        'de': 'Die Antwort wird gesendet…'},
    'Careful: the agent could have written these files, and tools run them or read them as configuration when you use the copy on the host (git, direnv, shells, editors, make): {list}. Look at them before you use the copied folder. Copied files are never executable on the host.': {
        'en': 'Careful: the agent could have written these files, and tools run them or read them as configuration when you use the copy on the host (git, direnv, shells, editors, make): {list}. Look at them before you use the copied folder. Copied files are never executable on the host.',
        'de': 'Vorsicht: Diese Dateien kann der Agent geschrieben haben, und Werkzeuge führen sie aus oder lesen sie als Konfiguration, wenn die Kopie auf dem Host benutzt wird (git, direnv, Shells, Editoren, make): {list}. Vor dem Benutzen des kopierten Ordners prüfen. Kopierte Dateien sind auf dem Host nie ausführbar.'},
    'Files that run or configure tools': {
        'en': 'Files that run or configure tools',
        'de': 'Dateien, die Werkzeuge ausführen oder konfigurieren'},
    '{path} has no .sandbox-ai.json.': {
        'en': '{path} has no .sandbox-ai.json.',
        'de': '{path} enthält keine .sandbox-ai.json.'},
    'Agent: {agent}': {
        'en': 'Agent: {agent}',
        'de': 'Agent: {agent}'},
    'It widens what the sandbox may do or see:': {
        'en': 'It widens what the sandbox may do or see:',
        'de': 'Sie erweitert, was die Sandbox darf oder sieht:'},
    'Further workspaces: {list}': {
        'en': 'Further workspaces: {list}',
        'de': 'Weitere Arbeitsordner: {list}'},
    'Agent arguments: {list}': {
        'en': 'Agent arguments: {list}',
        'de': 'Argumente für den Agenten: {list}'},
    'This project configuration cannot be used': {
        'en': 'This project configuration cannot be used',
        'de': 'Diese Projektkonfiguration kann nicht verwendet werden'},
    '{path} changed since you trusted it.': {
        'en': '{path} changed since you trusted it.',
        'de': '{path} wurde geändert, seit ihr vertraut wurde.'},
    '{path} is not trusted yet.': {
        'en': '{path} is not trusted yet.',
        'de': '{path} ist noch nicht vertrauenswürdig.'},
    'Trust exactly this content (see the details)? The launcher refuses the file if it changes afterwards.': {
        'en': 'Trust exactly this content (see the details)? The launcher refuses the file if it changes afterwards.',
        'de': 'Genau diesem Inhalt vertrauen (siehe Details)? Ändert sich die Datei danach, lehnt der Launcher sie ab.'},
    'Review the project configuration': {
        'en': 'Review the project configuration',
        'de': 'Projektkonfiguration prüfen'},
    'Trust': {
        'en': 'Trust',
        'de': 'Vertrauen'},
    'Not trusted': {
        'en': 'Not trusted',
        'de': 'Nicht vertraut'},
    "Trusted: {path}. The form starts in that folder; the launcher applies the file's options itself.": {
        'en': "Trusted: {path}. The form starts in that folder; the launcher applies the file's options itself.",
        'de': 'Vertraut: {path}. Das Formular startet in diesem Ordner; der Launcher wendet die Optionen der Datei selbst an.'},
    'Sandbox: {sandbox}': {
        'en': 'Sandbox: {sandbox}',
        'de': 'Sandbox: {sandbox}'},
    'Asks to: {question}': {
        'en': 'Asks to: {question}',
        'de': 'Fragt: {question}'},
    'Scope: {scope}': {
        'en': 'Scope: {scope}',
        'de': 'Geltung: {scope}'},
    '“Always” is remembered for this project: later runs get this without asking.': {
        'en': '“Always” is remembered for this project: later runs get this without asking.',
        'de': '„Immer“ wird für dieses Projekt gemerkt: spätere Läufe bekommen das ohne Nachfrage.'},
    'Allow this request?': {
        'en': 'Allow this request?',
        'de': 'Diese Anfrage erlauben?'},
    'Reading the logs…': {
        'en': 'Reading the logs…',
        'de': 'Die Logs werden gelesen…'},
    'Copying to the host…': {
        'en': 'Copying to the host…',
        'de': 'Wird auf den Host kopiert…'},
    "Copying into the sandbox's folder…": {
        'en': "Copying into the sandbox's folder…",
        'de': 'Wird in den Ordner der Sandbox kopiert…'},
    'Copied without the executable bit (make a file executable yourself once you trust it): {list}': {
        'en': 'Copied without the executable bit (make a file executable yourself once you trust it): {list}',
        'de': 'Ohne Ausführungsrecht kopiert (eine Datei erst selbst ausführbar machen, wenn ihr vertraut wird): {list}'},
    'No answer within {seconds} s.': {
        'en': 'No answer within {seconds} s.',
        'de': 'Keine Antwort innerhalb von {seconds} s.'},
    'Still at work: {pages}. Quit anyway? Copies are cancelled and what they created is removed; running sandbox-ai commands are stopped.': {
        'en': 'Still at work: {pages}. Quit anyway? Copies are cancelled and what they created is removed; running sandbox-ai commands are stopped.',
        'de': 'Noch in Arbeit: {pages}. Trotzdem beenden? Kopien werden abgebrochen und was sie angelegt haben entfernt; laufende sandbox-ai-Befehle werden beendet.'},
    'Stop and quit': {
        'en': 'Stop and quit',
        'de': 'Abbrechen und beenden'},
    'Keep working': {
        'en': 'Keep working',
        'de': 'Weiterarbeiten'},
    'Cannot read {path}: {error}': {
        'en': 'Cannot read {path}: {error}',
        'de': '{path} kann nicht gelesen werden: {error}'},
    'Keep answering in the system tray when the window is closed': {
        'en': 'Keep answering in the system tray when the window is closed',
        'de': 'Im Infobereich weiter antworten, wenn das Fenster geschlossen wird'},
    'Window': {
        'en': 'Window',
        'de': 'Fenster'},
    'Start at login, in the tray': {
        'en': 'Start at login, in the tray',
        'de': 'Bei der Anmeldung starten, im Infobereich'},
    'Writes ~/.config/autostart/sandbox-ai-app.desktop; unticked, it is removed': {
        'en': 'Writes ~/.config/autostart/sandbox-ai-app.desktop; unticked, it is removed',
        'de': 'Schreibt ~/.config/autostart/sandbox-ai-app.desktop; abgewählt wird sie entfernt'},
    '{count} sandbox(es) still run. Without this window nobody answers their questions: each is refused after {seconds} seconds. Quit anyway?': {
        'en': '{count} sandbox(es) still run. Without this window nobody answers their questions: each is refused after {seconds} seconds. Quit anyway?',
        'de': '{count} Sandbox(en) laufen noch. Ohne dieses Fenster beantwortet niemand ihre Fragen: jede wird nach {seconds} Sekunden abgelehnt. Trotzdem beenden?'},
    'Show sandbox-ai': {
        'en': 'Show sandbox-ai',
        'de': 'sandbox-ai anzeigen'},
    'There is no system tray here': {
        'en': 'There is no system tray here',
        'de': 'Hier gibt es keinen Infobereich'},
    'sandbox-ai keeps answering in the system tray; quit it from the tray menu.': {
        'en': 'sandbox-ai keeps answering in the system tray; quit it from the tray menu.',
        'de': 'sandbox-ai antwortet im Infobereich weiter; beenden über dessen Menü.'},
    'Keep the window': {
        'en': 'Keep the window',
        'de': 'Fenster behalten'},
    'Start at login': {
        'en': 'Start at login',
        'de': 'Bei der Anmeldung starten'},
    'Forget…': {
        'en': 'Forget…',
        'de': 'Vergessen…'},
    "The project's sandboxes ask again next time": {
        'en': "The project's sandboxes ask again next time",
        'de': 'Die Sandboxen des Projekts fragen beim nächsten Mal wieder'},
    'No details: {error}': {
        'en': 'No details: {error}',
        'de': 'Keine Details: {error}'},
    'Address {address}, port {port}, protocol {protocol}.': {
        'en': 'Address {address}, port {port}, protocol {protocol}.',
        'de': 'Adresse {address}, Port {port}, Protokoll {protocol}.'},
    'No earlier connections of this run to it in its log.': {
        'en': 'No earlier connections of this run to it in its log.',
        'de': 'Keine früheren Verbindungen dieses Laufs dorthin in seinem Log.'},
    'Project': {
        'en': 'Project',
        'de': 'Projekt'},
    'Allowed': {
        'en': 'Allowed',
        'de': 'Erlaubt'},
    'This sandbox-ai version cannot list them yet: update it.': {
        'en': 'This sandbox-ai version cannot list them yet: update it.',
        'de': 'Diese sandbox-ai-Version kann sie noch nicht auflisten: bitte aktualisieren.'},
    'Select an entry first.': {
        'en': 'Select an entry first.',
        'de': 'Zuerst einen Eintrag auswählen.'},
    'Forget': {
        'en': 'Forget',
        'de': 'Vergessen'},
    'Forgotten.': {
        'en': 'Forgotten.',
        'de': 'Vergessen.'},
    'The agent calls it {host}: not verified, the address is what is opened.': {
        'en': 'The agent calls it {host}: not verified, the address is what is opened.',
        'de': 'Der Agent nennt es {host}: nicht geprüft, geöffnet wird die Adresse.'},
    'Cannot read them: {error}': {
        'en': 'Cannot read them: {error}',
        'de': 'Nicht lesbar: {error}'},
    'Nothing is allowed always.': {
        'en': 'Nothing is allowed always.',
        'de': 'Nichts ist immer erlaubt.'},
    "Forget {entry} for {project}? The project's next sandboxes ask again. Sandboxes running now keep what they already allowed until they end (take it back there with Sandboxes › Revoke…, sandbox-ai net revoke).": {
        'en': "Forget {entry} for {project}? The project's next sandboxes ask again. Sandboxes running now keep what they already allowed until they end (take it back there with Sandboxes › Revoke…, sandbox-ai net revoke).",
        'de': '{entry} für {project} vergessen? Die nächsten Sandboxen des Projekts fragen wieder. Laufende Sandboxen behalten, was sie schon erlaubt bekommen haben, bis sie enden (dort zurücknehmen mit Sandboxen › Zurücknehmen…, sandbox-ai net revoke).'},
    'Not written: the app runs from {path}, inside {folder}, which a sandbox can write (it could change what starts at your next login). Install the app outside sandbox folders.': {
        'en': 'Not written: the app runs from {path}, inside {folder}, which a sandbox can write (it could change what starts at your next login). Install the app outside sandbox folders.',
        'de': 'Nicht geschrieben: die App läuft aus {path}, in {folder}, den eine Sandbox beschreiben kann (sie könnte ändern, was bei der nächsten Anmeldung startet). Die App außerhalb von Sandbox-Ordnern installieren.'},
    "Reading what the run's log knows about it…": {
        'en': "Reading what the run's log knows about it…",
        'de': 'Was das Log des Laufs darüber weiß, wird gelesen…'},
    'The agent wants to look up {name}. Nothing is looked up before you answer: the name itself can carry data out. Allowing resolves it and opens only its public addresses.': {
        'en': 'The agent wants to look up {name}. Nothing is looked up before you answer: the name itself can carry data out. Allowing resolves it and opens only its public addresses.',
        'de': 'Der Agent will {name} nachschlagen. Vor Ihrer Antwort wird nichts nachgeschlagen: schon der Name kann Daten hinaustragen. Erlauben löst ihn auf und öffnet nur seine öffentlichen Adressen.'},
    'Last connections of this run to it, from its log (text the agent chose, shown as it is):': {
        'en': 'Last connections of this run to it, from its log (text the agent chose, shown as it is):',
        'de': 'Letzte Verbindungen dieses Laufs dorthin, aus seinem Log (Text, den der Agent gewählt hat, unverändert gezeigt):'},
    'Careful: {path} lies in {folder}, which a sandbox can write or could write in an earlier run: an agent may have changed this code. Install the app and sandbox-ai outside every sandbox folder.': {
        'en': 'Careful: {path} lies in {folder}, which a sandbox can write or could write in an earlier run: an agent may have changed this code. Install the app and sandbox-ai outside every sandbox folder.',
        'de': 'Vorsicht: {path} liegt in {folder}, den eine Sandbox beschreiben kann oder in einem früheren Lauf beschreiben konnte: ein Agent kann diesen Code verändert haben. App und sandbox-ai außerhalb aller Sandbox-Ordner installieren.'},
    'sandbox-ai in a sandbox folder': {
        'en': 'sandbox-ai in a sandbox folder',
        'de': 'sandbox-ai in einem Sandbox-Ordner'},
    "Load sandbox-ai's code from there anyway?": {
        'en': "Load sandbox-ai's code from there anyway?",
        'de': 'Den Code von sandbox-ai trotzdem von dort laden?'},
    'Load anyway': {
        'en': 'Load anyway',
        'de': 'Trotzdem laden'},
    'Allowed or blocked always (per project)': {
        'en': 'Allowed or blocked always (per project)',
        'de': 'Immer erlaubt oder blockiert (je Projekt)'},
    'Block always': {
        'en': 'Block always',
        'de': 'Immer blockieren'},
    'allowed: address and port': {
        'en': 'allowed: address and port',
        'de': 'erlaubt: Adresse und Port'},
    'allowed: host name': {
        'en': 'allowed: host name',
        'de': 'erlaubt: Hostname'},
    'blocked: address and port': {
        'en': 'blocked: address and port',
        'de': 'blockiert: Adresse und Port'},
    'blocked: host name': {
        'en': 'blocked: host name',
        'de': 'blockiert: Hostname'},
    '--local-config is for OpenCode only.': {
        'en': '--local-config is for OpenCode only.',
        'de': '--local-config ist nur für OpenCode.'},
    '--no-inspect has no effect without the connection proxy.': {
        'en': '--no-inspect has no effect without the connection proxy.',
        'de': '--no-inspect wirkt ohne Verbindungs-Proxy nicht.'},
    'A folder the agent works in: the first at /work, further ones at /workspaces/NAME': {
        'en': 'A folder the agent works in: the first at /work, further ones at /workspaces/NAME',
        'de': 'Ein Ordner, in dem der Agent arbeitet: der erste unter /work, weitere unter /workspaces/NAME'},
    'A further host folder or file at /work, /mnt or /opt, read-only or writable': {
        'en': 'A further host folder or file at /work, /mnt or /opt, read-only or writable',
        'de': 'Ein weiterer Ordner oder eine Datei des Hosts unter /work, /mnt oder /opt, nur lesbar oder beschreibbar'},
    'A local MCP server in its own sandbox, with its own network and folders': {
        'en': 'A local MCP server in its own sandbox, with its own network and folders',
        'de': 'Ein lokaler MCP-Server in eigener Sandbox, mit eigenem Netzwerk und eigenen Ordnern'},
    'A mount goes under /work, /mnt or /opt.': {
        'en': 'A mount goes under /work, /mnt or /opt.',
        'de': 'Eine Einbindung gehört unter /work, /mnt oder /opt.'},
    'A mount is read-only (ro) or writable (rw).': {
        'en': 'A mount is read-only (ro) or writable (rw).',
        'de': 'Eine Einbindung ist nur lesbar (ro) oder beschreibbar (rw).'},
    "A request rule is 'HOST METHODS PATH', e.g. 'api.github.com GET,HEAD /repos/*': {rule}": {
        'en': "A request rule is 'HOST METHODS PATH', e.g. 'api.github.com GET,HEAD /repos/*': {rule}",
        'de': "Eine Anfrageregel ist 'HOST METHODEN PFAD', z. B. 'api.github.com GET,HEAD /repos/*': {rule}"},
    'A resolver is one IP address: {value}': {
        'en': 'A resolver is one IP address: {value}',
        'de': 'Ein Resolver ist eine IP-Adresse: {value}'},
    'A virtual display for GUI programs and browsers': {
        'en': 'A virtual display for GUI programs and browsers',
        'de': 'Ein virtueller Bildschirm für GUI-Programme und Browser'},
    'A workspace needs a folder.': {
        'en': 'A workspace needs a folder.',
        'de': 'Ein Arbeitsordner braucht einen Ordner.'},
    'A workspace path cannot contain commas or new lines.': {
        'en': 'A workspace path cannot contain commas or new lines.',
        'de': 'Ein Arbeitsordner-Pfad darf keine Kommas oder Zeilenumbrüche enthalten.'},
    'Commit identity and push tokens (with the token proxy the agent never sees them)': {
        'en': 'Commit identity and push tokens (with the token proxy the agent never sees them)',
        'de': 'Commit-Identität und Push-Tokens (mit dem Token-Proxy sieht der Agent sie nie)'},
    'Connection proxy': {
        'en': 'Connection proxy',
        'de': 'Verbindungs-Proxy'},
    'Connection proxy: TLS inspected, every request logged': {
        'en': 'Connection proxy: TLS inspected, every request logged',
        'de': 'Verbindungs-Proxy: TLS geprüft, jede Anfrage protokolliert'},
    'Copy the login or copy none: not both.': {
        'en': 'Copy the login or copy none: not both.',
        'de': 'Die Anmeldung kopieren oder keine: nicht beides.'},
    'Devices and GPU': {
        'en': 'Devices and GPU',
        'de': 'Geräte und GPU'},
    'Display': {
        'en': 'Display',
        'de': 'Bildschirm'},
    'Egress-all: every outbound destination': {
        'en': 'Egress-all: every outbound destination',
        'de': 'Egress-all: jedes ausgehende Ziel'},
    'Every connection passes it: TLS opened and logged, pushes checked, requests per host': {
        'en': 'Every connection passes it: TLS opened and logged, pushes checked, requests per host',
        'de': 'Jede Verbindung läuft hindurch: TLS geöffnet und protokolliert, Pushes geprüft, Anfragen je Host'},
    'Git tokens need network access to their server; offline has none.': {
        'en': 'Git tokens need network access to their server; offline has none.',
        'de': 'Git-Tokens brauchen Netzwerkzugang zu ihrem Server; offline gibt es keinen.'},
    'Git tokens only in the proxy: the agent never sees them': {
        'en': 'Git tokens only in the proxy: the agent never sees them',
        'de': 'Git-Tokens nur im Proxy: der Agent sieht sie nie'},
    'HOME and login': {
        'en': 'HOME and login',
        'de': 'HOME und Anmeldung'},
    'Host devices handed to the agent (weakens the isolation)': {
        'en': 'Host devices handed to the agent (weakens the isolation)',
        'de': 'Geräte des Hosts für den Agenten (schwächt die Isolation)'},
    'VPN': {
        'en': 'VPN',
        'de': 'VPN'},
    'A VPN profile in front (sandbox-ai vpn): everything leaves through the tunnel, a kill switch blocks the rest, addresses and DNS are checked before the start': {
        'en': 'A VPN profile in front (sandbox-ai vpn): everything leaves through the tunnel, a kill switch blocks the rest, addresses and DNS are checked before the start',
        'de': 'Ein VPN-Profil davor (sandbox-ai vpn): alles geht durch den Tunnel, ein Kill Switch sperrt den Rest, Adressen und DNS werden vor dem Start geprüft'},
    'A VPN needs the Podman backend.': {
        'en': 'A VPN needs the Podman backend.',
        'de': 'Ein VPN braucht das Podman-Backend.'},
    'A VPN needs network; offline has none.': {
        'en': 'A VPN needs network; offline has none.',
        'de': 'Ein VPN braucht Netz; offline gibt es keines.'},
    'With a VPN the lookups go to its resolvers: no --nameserver.': {
        'en': 'With a VPN the lookups go to its resolvers: no --nameserver.',
        'de': 'Mit einem VPN gehen die Anfragen an seine Resolver: kein --nameserver.'},
    'The check mode has no effect without a VPN profile.': {
        'en': 'The check mode has no effect without a VPN profile.',
        'de': 'Der Prüfmodus wirkt nur mit einem VPN-Profil.'},
    "The administrator's policy requires a VPN.": {
        'en': "The administrator's policy requires a VPN.",
        'de': 'Die Richtlinie des Administrators verlangt ein VPN.'},
    "The administrator's policy allows only these VPN profiles: {list}.": {
        'en': "The administrator's policy allows only these VPN profiles: {list}.",
        'de': 'Die Richtlinie des Administrators erlaubt nur diese VPN-Profile: {list}.'},
    "The administrator's policy requires a VPN: failed checks stop the start.": {
        'en': "The administrator's policy requires a VPN: failed checks stop the start.",
        'de': 'Die Richtlinie des Administrators verlangt ein VPN: fehlgeschlagene Prüfungen stoppen den Start.'},
    'Host ports need the Podman backend with its default network.': {
        'en': 'Host ports need the Podman backend with its default network.',
        'de': 'Host-Ports brauchen das Podman-Backend mit seinem Standardnetzwerk.'},
    'Kits copied into HOME, project memory and plugins': {
        'en': 'Kits copied into HOME, project memory and plugins',
        'de': 'Kits, die ins HOME kopiert werden, Projektgedächtnis und Plugins'},
    'Kits, memory, plugins': {
        'en': 'Kits, memory, plugins',
        'de': 'Kits, Gedächtnis, Plugins'},
    'Locked down (offline)': {
        'en': 'Locked down (offline)',
        'de': 'Abgeschottet (offline)'},
    'MCP server': {
        'en': 'MCP server',
        'de': 'MCP-Server'},
    'Memory, CPUs and processes': {
        'en': 'Memory, CPUs and processes',
        'de': 'Speicher, CPUs und Prozesse'},
    'Mount': {
        'en': 'Mount',
        'de': 'Einbindung'},
    'No connection proxy: no connection log, pushes checked only by the hooks': {
        'en': 'No connection proxy: no connection log, pushes checked only by the hooks',
        'de': 'Kein Verbindungs-Proxy: kein Verbindungslog, Pushes nur von den Hooks geprüft'},
    'No workspace: the folder the sandbox is started in becomes /work.': {
        'en': 'No workspace: the folder the sandbox is started in becomes /work.',
        'de': 'Kein Arbeitsordner: der Ordner, in dem die Sandbox gestartet wird, wird /work.'},
    'Not a port: {value}': {
        'en': 'Not a port: {value}',
        'de': 'Kein Port: {value}'},
    'Not an address or network: {value}': {
        'en': 'Not an address or network: {value}',
        'de': 'Keine Adresse und kein Netz: {value}'},
    'Nothing is asked: what the options do not allow is refused': {
        'en': 'Nothing is asked: what the options do not allow is refused',
        'de': 'Es wird nichts gefragt: was die Optionen nicht erlauben, wird abgelehnt'},
    'Offline contradicts the network permissions: {options}.': {
        'en': 'Offline contradicts the network permissions: {options}.',
        'de': 'Offline widerspricht den Netzwerkfreigaben: {options}.'},
    'Offline: no network at all': {
        'en': 'Offline: no network at all',
        'de': 'Offline: überhaupt kein Netzwerk'},
    "Podman or Bubblewrap, the image and the agent's executable": {
        'en': "Podman or Bubblewrap, the image and the agent's executable",
        'de': 'Podman oder Bubblewrap, das Image und das Programm des Agenten'},
    'Privacy check off: pushes and SSH are not checked': {
        'en': 'Privacy check off: pushes and SSH are not checked',
        'de': 'Datenschutzprüfung aus: Pushes und SSH werden nicht geprüft'},
    'Read-only applies to every workspace (--workspace-ro), not to one.': {
        'en': 'Read-only applies to every workspace (--workspace-ro), not to one.',
        'de': 'Nur-lesbar gilt für jeden Arbeitsordner (--workspace-ro), nicht für einen.'},
    'Real DNS lookups (a way out for small amounts of data)': {
        'en': 'Real DNS lookups (a way out for small amounts of data)',
        'de': 'Echte DNS-Abfragen (ein Weg hinaus für kleine Datenmengen)'},
    'Requests narrowed per host ({count} rules)': {
        'en': 'Requests narrowed per host ({count} rules)',
        'de': 'Anfragen je Host eingeschränkt ({count} Regeln)'},
    'Requests per host (--allow-http) need the connection proxy.': {
        'en': 'Requests per host (--allow-http) need the connection proxy.',
        'de': 'Anfragen je Host (--allow-http) brauchen den Verbindungs-Proxy.'},
    'Requests to {host} are not seen under --no-inspect, so rules for it cannot hold.': {
        'en': 'Requests to {host} are not seen under --no-inspect, so rules for it cannot hold.',
        'de': 'Anfragen an {host} sind unter --no-inspect nicht sichtbar, Regeln dafür können also nicht greifen.'},
    'Session and tmux': {
        'en': 'Session and tmux',
        'de': 'Sitzung und tmux'},
    'TLS not inspected for {host}': {
        'en': 'TLS not inspected for {host}',
        'de': 'TLS für {host} nicht geprüft'},
    "The administrator's policy allows only these backends: {list}.": {
        'en': "The administrator's policy allows only these backends: {list}.",
        'de': 'Die Richtlinie des Administrators erlaubt nur diese Backends: {list}.'},
    "The administrator's policy does not list {host} in allow_hosts.": {
        'en': "The administrator's policy does not list {host} in allow_hosts.",
        'de': 'Die Richtlinie des Administrators führt {host} nicht in allow_hosts.'},
    "The administrator's policy forbids {option}.": {
        'en': "The administrator's policy forbids {option}.",
        'de': 'Die Richtlinie des Administrators verbietet {option}.'},
    "The administrator's policy has a network ceiling: no egress-all.": {
        'en': "The administrator's policy has a network ceiling: no egress-all.",
        'de': 'Die Richtlinie des Administrators hat eine Netzwerkgrenze: kein egress-all.'},
    'The agent runs without its own approval prompts (the sandbox stays in force)': {
        'en': 'The agent runs without its own approval prompts (the sandbox stays in force)',
        'de': 'Der Agent läuft ohne eigene Rückfragen (die Sandbox gilt weiter)'},
    'The coding agent and how it runs': {
        'en': 'The coding agent and how it runs',
        'de': 'Der Coding-Agent und wie er läuft'},
    "The host's Wayland socket weakens the isolation": {
        'en': "The host's Wayland socket weakens the isolation",
        'de': 'Der Wayland-Socket des Hosts schwächt die Isolation'},
    "The sandbox HOME (kept between runs or not) and the agent's login": {
        'en': "The sandbox HOME (kept between runs or not) and the agent's login",
        'de': 'Das HOME der Sandbox (zwischen Läufen behalten oder nicht) und die Anmeldung des Agenten'},
    'The token proxy adds tokens only to HTTPS servers: {url}': {
        'en': 'The token proxy adds tokens only to HTTPS servers: {url}',
        'de': 'Der Token-Proxy fügt Tokens nur bei HTTPS-Servern hinzu: {url}'},
    'The token proxy needs the connection proxy.': {
        'en': 'The token proxy needs the connection proxy.',
        'de': 'Der Token-Proxy braucht den Verbindungs-Proxy.'},
    'The token proxy takes --git-token-env, --git-token-file or --git-token-secret, not --git-credentials.': {
        'en': 'The token proxy takes --git-token-env, --git-token-file or --git-token-secret, not --git-credentials.',
        'de': 'Der Token-Proxy nimmt --git-token-env, --git-token-file oder --git-token-secret, nicht --git-credentials.'},
    'Where connections may go: hosts, presets, addresses, ports, DNS': {
        'en': 'Where connections may go: hosts, presets, addresses, ports, DNS',
        'de': 'Wohin Verbindungen dürfen: Hosts, Vorgaben, Adressen, Ports, DNS'},
    'tmux cannot be both required and switched off.': {
        'en': 'tmux cannot be both required and switched off.',
        'de': 'tmux kann nicht zugleich verlangt und abgeschaltet sein.'},
    'tmux inside the sandbox (sessions are started from the Start page)': {
        'en': 'tmux inside the sandbox (sessions are started from the Start page)',
        'de': 'tmux in der Sandbox (Sitzungen werden auf der Startseite gestartet)'},
    '{name} is not connected to the agent, so it is not used.': {
        'en': '{name} is not connected to the agent, so it is not used.',
        'de': '{name} ist nicht mit dem Agenten verbunden und wird daher nicht genutzt.'},
    "{value} lies outside the administrator's network ceiling.": {
        'en': "{value} lies outside the administrator's network ceiling.",
        'de': '{value} liegt außerhalb der Netzwerkgrenze des Administrators.'},
    'Definition': {
        'en': 'Definition',
        'de': 'Definition'},
    'Folders': {
        'en': 'Folders',
        'de': 'Ordner'},
    'Hosts': {
        'en': 'Hosts',
        'de': 'Hosts'},
    'none': {
        'en': 'none',
        'de': 'keine'},
    'not defined: add it on the MCP servers page': {
        'en': 'not defined: add it on the MCP servers page',
        'de': 'nicht definiert: auf der Seite MCP-Server hinzufügen'},
    'Add a workspace first: the file is written there.': {
        'en': 'Add a workspace first: the file is written there.',
        'de': 'Zuerst einen Arbeitsordner hinzufügen: die Datei wird dort geschrieben.'},
    'Add rule': {
        'en': 'Add rule',
        'de': 'Regel hinzufügen'},
    'Apply…': {
        'en': 'Apply…',
        'de': 'Übernehmen…'},
    'Arrange': {
        'en': 'Arrange',
        'de': 'Anordnen'},
    'Board': {
        'en': 'Board',
        'de': 'Tafel'},
    'Change {name}': {
        'en': 'Change {name}',
        'de': '{name} ändern'},
    'Connect': {
        'en': 'Connect',
        'de': 'Verbinden'},
    'Drag from here to an input port to connect': {
        'en': 'Drag from here to an input port to connect',
        'de': 'Von hier zu einem Eingang ziehen, um zu verbinden'},
    'Fit': {
        'en': 'Fit',
        'de': 'Einpassen'},
    'Host folder or file': {
        'en': 'Host folder or file',
        'de': 'Ordner oder Datei des Hosts'},
    'In the sandbox': {
        'en': 'In the sandbox',
        'de': 'In der Sandbox'},
    'Lay the nodes out in columns: files, agent, proxy, network': {
        'en': 'Lay the nodes out in columns: files, agent, proxy, network',
        'de': 'Die Knoten in Spalten anordnen: Dateien, Agent, Proxy, Netzwerk'},
    'Methods': {
        'en': 'Methods',
        'de': 'Methoden'},
    'Mode': {
        'en': 'Mode',
        'de': 'Modus'},
    'Move': {
        'en': 'Move',
        'de': 'Verschieben'},
    'No project folder': {
        'en': 'No project folder',
        'de': 'Kein Projektordner'},
    'Path': {
        'en': 'Path',
        'de': 'Pfad'},
    'Project folder': {
        'en': 'Project folder',
        'de': 'Projektordner'},
    'Property': {
        'en': 'Property',
        'de': 'Eigenschaft'},
    'Redo': {
        'en': 'Redo',
        'de': 'Wiederholen'},
    'Remove rule': {
        'en': 'Remove rule',
        'de': 'Regel entfernen'},
    'Requests per host (--allow-http)': {
        'en': 'Requests per host (--allow-http)',
        'de': 'Anfragen je Host (--allow-http)'},
    'Security score: {score} of 100': {
        'en': 'Security score: {score} of 100',
        'de': 'Sicherheitswert: {score} von 100'},
    'Select a part to change it': {
        'en': 'Select a part to change it',
        'de': 'Einen Teil auswählen, um ihn zu ändern'},
    'Select and press Delete to disconnect': {
        'en': 'Select and press Delete to disconnect',
        'de': 'Auswählen und Entf drücken, um zu trennen'},
    "The administrator's policy forbids --no-proxy": {
        'en': "The administrator's policy forbids --no-proxy",
        'de': 'Die Richtlinie des Administrators verbietet --no-proxy'},
    "The administrator's policy forbids this": {
        'en': "The administrator's policy forbids this",
        'de': 'Die Richtlinie des Administrators verbietet das'},
    "The administrator's policy forbids {option}": {
        'en': "The administrator's policy forbids {option}",
        'de': 'Die Richtlinie des Administrators verbietet {option}'},
    'The launcher accepts these options.': {
        'en': 'The launcher accepts these options.',
        'de': 'Der Launcher akzeptiert diese Optionen.'},
    'The launcher checks the options…': {
        'en': 'The launcher checks the options…',
        'de': 'Der Launcher prüft die Optionen…'},
    'The launcher refuses: {message}': {
        'en': 'The launcher refuses: {message}',
        'de': 'Der Launcher lehnt ab: {message}'},
    'Undo': {
        'en': 'Undo',
        'de': 'Rückgängig'},
    'Write .sandbox-ai.json into the first workspace and trust it, after showing it and what changes': {
        'en': 'Write .sandbox-ai.json into the first workspace and trust it, after showing it and what changes',
        'de': '.sandbox-ai.json in den ersten Arbeitsordner schreiben und ihr vertrauen, nachdem sie und die Änderungen gezeigt wurden'},
    'Write {path} with this content and trust exactly it? The details show what changes.': {
        'en': 'Write {path} with this content and trust exactly it? The details show what changes.',
        'de': '{path} mit diesem Inhalt schreiben und genau ihm vertrauen? Die Details zeigen, was sich ändert.'},
    'after': {
        'en': 'after',
        'de': 'nachher'},
    'before': {
        'en': 'before',
        'de': 'vorher'},
    'defaults': {
        'en': 'defaults',
        'de': 'Vorgaben'},
    'e.g. --verbose': {
        'en': 'e.g. --verbose',
        'de': 'z. B. --verbose'},
    'every workspace read-only (--workspace-ro)': {
        'en': 'every workspace read-only (--workspace-ro)',
        'de': 'jeder Arbeitsordner nur lesbar (--workspace-ro)'},
    'offline': {
        'en': 'offline',
        'de': 'offline'},
    'on (every connection passes it; off: --no-proxy)': {
        'en': 'on (every connection passes it; off: --no-proxy)',
        'de': 'an (jede Verbindung läuft hindurch; aus: --no-proxy)'},
    'on, TLS inspected': {
        'en': 'on, TLS inspected',
        'de': 'an, TLS geprüft'},
    'on, {count} request rules': {
        'en': 'on, {count} request rules',
        'de': 'an, {count} Anfrageregeln'},
    'passed as --model to the agent': {
        'en': 'passed as --model to the agent',
        'de': 'als --model an den Agenten übergeben'},
    'read-only': {
        'en': 'read-only',
        'de': 'nur lesbar'},
    "the agent's provider only": {
        'en': "the agent's provider only",
        'de': 'nur der Anbieter des Agenten'},
    '{path} has no .sandbox-ai.json yet: Apply writes one.': {
        'en': '{path} has no .sandbox-ai.json yet: Apply writes one.',
        'de': '{path} hat noch keine .sandbox-ai.json: Übernehmen schreibt eine.'},
    '{path} is not trusted yet: the board shows it; Apply trusts what you see after you review it.': {
        'en': '{path} is not trusted yet: the board shows it; Apply trusts what you see after you review it.',
        'de': '{path} ist noch nicht vertrauenswürdig: die Tafel zeigt sie; Übernehmen vertraut dem Gezeigten nach Ihrer Prüfung.'},
    'A new project step by step; it ends on this board': {
        'en': 'A new project step by step; it ends on this board',
        'de': 'Ein neues Projekt Schritt für Schritt; es endet auf dieser Tafel'},
    'Add token': {
        'en': 'Add token',
        'de': 'Token hinzufügen'},
    'CPUs': {
        'en': 'CPUs',
        'de': 'CPUs'},
    'Command line': {
        'en': 'Command line',
        'de': 'Befehlszeile'},
    'Connection proxy and TLS': {
        'en': 'Connection proxy and TLS',
        'de': 'Verbindungs-Proxy und TLS'},
    'Custom (the defaults)': {
        'en': 'Custom (the defaults)',
        'de': 'Eigene (die Vorgaben)'},
    'Devices (/dev/...)': {
        'en': 'Devices (/dev/...)',
        'de': 'Geräte (/dev/...)'},
    'Devices and limits': {
        'en': 'Devices and limits',
        'de': 'Geräte und Grenzen'},
    'Devices weaken the isolation: a service on the host reached through a host port is safer.': {
        'en': 'Devices weaken the isolation: a service on the host reached through a host port is safer.',
        'de': 'Geräte schwächen die Isolation: ein Dienst auf dem Host über einen Host-Port ist sicherer.'},
    'Each token needs a server (https://HOST) and a name, file or token.': {
        'en': 'Each token needs a server (https://HOST) and a name, file or token.',
        'de': 'Jedes Token braucht einen Server (https://HOST) und einen Namen, eine Datei oder ein Token.'},
    'Every connection passes the proxy: TLS is opened with a CA for this run, every request is logged, pushes are checked.': {
        'en': 'Every connection passes the proxy: TLS is opened with a CA for this run, every request is logged, pushes are checked.',
        'de': 'Jede Verbindung läuft durch den Proxy: TLS wird mit einer CA für diesen Lauf geöffnet, jede Anfrage protokolliert, Pushes werden geprüft.'},
    'Further hosts (HTTPS)': {
        'en': 'Further hosts (HTTPS)',
        'de': 'Weitere Hosts (HTTPS)'},
    'GPU': {
        'en': 'GPU',
        'de': 'GPU'},
    'Git and tokens': {
        'en': 'Git and tokens',
        'de': 'Git und Tokens'},
    'Git server': {
        'en': 'Git server',
        'de': 'Git-Server'},
    'Hosts (or *.domain) whose TLS passes unopened: only their names are logged': {
        'en': 'Hosts (or *.domain) whose TLS passes unopened: only their names are logged',
        'de': 'Hosts (oder *.domain), deren TLS ungeöffnet durchgeht: nur ihre Namen werden protokolliert'},
    'Local MCP servers the agent may use; each runs in its own sandbox with its own network and folders (MCP servers page).': {
        'en': 'Local MCP servers the agent may use; each runs in its own sandbox with its own network and folders (MCP servers page).',
        'de': 'Lokale MCP-Server, die der Agent nutzen darf; jeder läuft in eigener Sandbox mit eigenem Netzwerk und eigenen Ordnern (Seite MCP-Server).'},
    'Name, file or token': {
        'en': 'Name, file or token',
        'de': 'Name, Datei oder Token'},
    'New sandbox for a project': {
        'en': 'New sandbox for a project',
        'de': 'Neue Sandbox für ein Projekt'},
    'No MCP server is defined yet.': {
        'en': 'No MCP server is defined yet.',
        'de': 'Noch kein MCP-Server definiert.'},
    'No change.': {
        'en': 'No change.',
        'de': 'Keine Änderung.'},
    'Ports of services on this host': {
        'en': 'Ports of services on this host',
        'de': 'Ports von Diensten auf diesem Host'},
    'Presets': {
        'en': 'Presets',
        'de': 'Vorgaben'},
    'Privacy check of pushes': {
        'en': 'Privacy check of pushes',
        'de': 'Datenschutzprüfung von Pushes'},
    'Profiles': {
        'en': 'Profiles',
        'de': 'Profile'},
    "Push from the sandbox: a typed token goes to a private file, never into the project file. The project's own remote is not offered: the agent can rewrite .git/config.": {
        'en': "Push from the sandbox: a typed token goes to a private file, never into the project file. The project's own remote is not offered: the agent can rewrite .git/config.",
        'de': 'Aus der Sandbox pushen: ein eingetipptes Token kommt in eine private Datei, nie in die Projektdatei. Das eigene Remote des Projekts wird nicht angeboten: der Agent kann .git/config umschreiben.'},
    'Remove token': {
        'en': 'Remove token',
        'de': 'Token entfernen'},
    'Review': {
        'en': 'Review',
        'de': 'Prüfen'},
    'Start from': {
        'en': 'Start from',
        'de': 'Ausgangspunkt'},
    'TLS not opened for': {
        'en': 'TLS not opened for',
        'de': 'TLS nicht geöffnet für'},
    'The folder the agent works in (/work in the sandbox); the project file is written there.': {
        'en': 'The folder the agent works in (/work in the sandbox); the project file is written there.',
        'de': 'Der Ordner, in dem der Agent arbeitet (/work in der Sandbox); die Projektdatei wird dort geschrieben.'},
    'The project file and what changes': {
        'en': 'The project file and what changes',
        'de': 'Die Projektdatei und was sich ändert'},
    'The project file there cannot be read: {error}': {
        'en': 'The project file there cannot be read: {error}',
        'de': 'Die Projektdatei dort ist nicht lesbar: {error}'},
    "The project's current configuration": {
        'en': "The project's current configuration",
        'de': 'Die aktuelle Konfiguration des Projekts'},
    'The token was not kept: {error}': {
        'en': 'The token was not kept: {error}',
        'de': 'Das Token wurde nicht gespeichert: {error}'},
    'Token from': {
        'en': 'Token from',
        'de': 'Token aus'},
    'What will be written and started. Finish writes the project file, trusts exactly it and opens the board.': {
        'en': 'What will be written and started. Finish writes the project file, trusts exactly it and opens the board.',
        'de': 'Was geschrieben und gestartet wird. Fertigstellen schreibt die Projektdatei, vertraut genau ihr und öffnet die Tafel.'},
    "Where the agent's connections may go (its provider is always allowed unless switched off).": {
        'en': "Where the agent's connections may go (its provider is always allowed unless switched off).",
        'de': 'Wohin die Verbindungen des Agenten dürfen (sein Anbieter ist immer erlaubt, außer abgeschaltet).'},
    'Which coding agent runs in the sandbox.': {
        'en': 'Which coding agent runs in the sandbox.',
        'de': 'Welcher Coding-Agent in der Sandbox läuft.'},
    'Wizard…': {
        'en': 'Wizard…',
        'de': 'Assistent…'},
    'ask before anything else (approvals)': {
        'en': 'ask before anything else (approvals)',
        'de': 'bei allem anderen nachfragen (Freigaben)'},
    'commit as my global user.name and user.email (--git-identity)': {
        'en': 'commit as my global user.name and user.email (--git-identity)',
        'de': 'mit meinem globalen user.name und user.email committen (--git-identity)'},
    'connection proxy on': {
        'en': 'connection proxy on',
        'de': 'Verbindungs-Proxy an'},
    'half of the CPUs': {
        'en': 'half of the CPUs',
        'de': 'die Hälfte der CPUs'},
    'host variable': {
        'en': 'host variable',
        'de': 'Host-Variable'},
    "keep the tokens out of the sandbox: the connection proxy adds them to Git's requests (--token-proxy)": {
        'en': "keep the tokens out of the sandbox: the connection proxy adds them to Git's requests (--token-proxy)",
        'de': 'die Tokens aus der Sandbox heraushalten: der Verbindungs-Proxy fügt sie Gits Anfragen hinzu (--token-proxy)'},
    'stored secret': {
        'en': 'stored secret',
        'de': 'gespeichertes Geheimnis'},
    "the agent's default": {
        'en': "the agent's default",
        'de': 'die Vorgabe des Agenten'},
    'token file': {
        'en': 'token file',
        'de': 'Token-Datei'},
    'typed here (kept in a private file)': {
        'en': 'typed here (kept in a private file)',
        'de': 'hier eingetippt (in einer privaten Datei gespeichert)'},
    "without the agent's own approval prompts (-D; the sandbox stays in force)": {
        'en': "without the agent's own approval prompts (-D; the sandbox stays in force)",
        'de': 'ohne die eigenen Rückfragen des Agenten (-D; die Sandbox gilt weiter)'},
    'Add device': {
        'en': 'Add device',
        'de': 'Gerät hinzufügen'},
    'Add devices to the node first.': {
        'en': 'Add devices to the node first.',
        'de': 'Zuerst Geräte am Knoten eintragen.'},
    'Add device…': {
        'en': 'Add device…',
        'de': 'Gerät hinzufügen…'},
    'Add these devices to {run} now': {
        'en': 'Add these devices to {run} now',
        'de': 'Diese Geräte jetzt {run} geben'},
    'Added during the run: {list}': {
        'en': 'Added during the run: {list}',
        'de': 'Während des Laufs hinzugefügt: {list}'},
    'Apply these limits to {run} now': {
        'en': 'Apply these limits to {run} now',
        'de': 'Diese Grenzen jetzt für {run} setzen'},
    'CPUs (e.g. 2 or 1.5)': {
        'en': 'CPUs (e.g. 2 or 1.5)',
        'de': 'CPUs (z. B. 2 oder 1.5)'},
    'Change memory, CPUs and processes of the selected sandbox': {
        'en': 'Change memory, CPUs and processes of the selected sandbox',
        'de': 'Speicher, CPUs und Prozesse der gewählten Sandbox ändern'},
    'Device': {
        'en': 'Device',
        'de': 'Gerät'},
    'Devices weaken the isolation: a service on the host reached through a host port is safer. Refused devices are greyed out.': {
        'en': 'Devices weaken the isolation: a service on the host reached through a host port is safer. Refused devices are greyed out.',
        'de': 'Geräte schwächen die Isolation: ein Dienst auf dem Host über einen Host-Port ist sicherer. Abgelehnte Geräte sind ausgegraut.'},
    'For the rest of the run; empty: unchanged, none: no limit.': {
        'en': 'For the rest of the run; empty: unchanged, none: no limit.',
        'de': 'Für den Rest des Laufs; leer: unverändert, none: keine Grenze.'},
    'Give {list} to {run} until it ends? Devices weaken the isolation.': {
        'en': 'Give {list} to {run} until it ends? Devices weaken the isolation.',
        'de': '{list} an {run} geben, bis sie endet? Geräte schwächen die Isolation.'},
    'Give {path} to {run} until it ends? Devices weaken the isolation.': {
        'en': 'Give {path} to {run} until it ends? Devices weaken the isolation.',
        'de': '{path} an {run} geben, bis sie endet? Geräte schwächen die Isolation.'},
    'Hand a host device to the selected sandbox until it ends': {
        'en': 'Hand a host device to the selected sandbox until it ends',
        'de': 'Der gewählten Sandbox ein Gerät des Hosts geben, bis sie endet'},
    'Limits of {run}': {
        'en': 'Limits of {run}',
        'de': 'Grenzen von {run}'},
    'Limits…': {
        'en': 'Limits…',
        'de': 'Grenzen…'},
    'Live, until the sandbox ends: {run} runs this project now.': {
        'en': 'Live, until the sandbox ends: {run} runs this project now.',
        'de': 'Live, bis die Sandbox endet: {run} führt dieses Projekt gerade aus.'},
    'Memory (e.g. 8g)': {
        'en': 'Memory (e.g. 8g)',
        'de': 'Speicher (z. B. 8g)'},
    'No device added during the run.': {
        'en': 'No device added during the run.',
        'de': 'Kein Gerät während des Laufs hinzugefügt.'},
    'No device was added to {run} during its run (devices given at the start stay).': {
        'en': 'No device was added to {run} during its run (devices given at the start stay).',
        'de': '{run} wurde während des Laufs kein Gerät hinzugefügt (beim Start gegebene Geräte bleiben).'},
    'Not added': {
        'en': 'Not added',
        'de': 'Nicht hinzugefügt'},
    'Remove device': {
        'en': 'Remove device',
        'de': 'Gerät entfernen'},
    'Set a limit on the node first.': {
        'en': 'Set a limit on the node first.',
        'de': 'Zuerst eine Grenze am Knoten setzen.'},
    'Take back a device added during the run': {
        'en': 'Take back a device added during the run',
        'de': 'Ein während des Laufs hinzugefügtes Gerät zurücknehmen'},
    'This sandbox-ai version cannot change devices during a run.': {
        'en': 'This sandbox-ai version cannot change devices during a run.',
        'de': 'Diese sandbox-ai-Version kann Geräte während eines Laufs nicht ändern.'},
    'sandbox-ai refuses it: {why}': {
        'en': 'sandbox-ai refuses it: {why}',
        'de': 'sandbox-ai lehnt es ab: {why}'},
    'A new language applies when the app is started again.': {
        'en': 'A new language applies when the app is started again.',
        'de': 'Eine neue Sprache gilt ab dem nächsten Start der App.'},
    'Administrator policy': {
        'en': 'Administrator policy',
        'de': 'Richtlinie des Administrators'},
    'All settings…': {
        'en': 'All settings…',
        'de': 'Alle Einstellungen…'},
    'Appearance': {
        'en': 'Appearance',
        'de': 'Darstellung'},
    'Backends': {
        'en': 'Backends',
        'de': 'Backends'},
    'Board grid (0: no snapping)': {
        'en': 'Board grid (0: no snapping)',
        'de': 'Raster der Tafel (0: kein Einrasten)'},
    'Connection log (milliseconds)': {
        'en': 'Connection log (milliseconds)',
        'de': 'Verbindungslog (Millisekunden)'},
    'Desktop notification for each new question': {
        'en': 'Desktop notification for each new question',
        'de': 'Desktop-Benachrichtigung für jede neue Frage'},
    'Empty: the sandbox-ai command, or $SANDBOX_AI_HOME when it is set (it wins). Applies at the next start; a folder a sandbox can write is refused then.': {
        'en': 'Empty: the sandbox-ai command, or $SANDBOX_AI_HOME when it is set (it wins). Applies at the next start; a folder a sandbox can write is refused then.',
        'de': 'Leer: der Befehl sandbox-ai, oder $SANDBOX_AI_HOME, wenn gesetzt (hat Vorrang). Gilt ab dem nächsten Start; ein Ordner, den eine Sandbox beschreiben kann, wird dann abgelehnt.'},
    'Forbidden': {
        'en': 'Forbidden',
        'de': 'Verboten'},
    'Forbidden: {list}; widest answer: {scope}.': {
        'en': 'Forbidden: {list}; widest answer: {scope}.',
        'de': 'Verboten: {list}; weiteste Antwort: {scope}.'},
    'In use': {
        'en': 'In use',
        'de': 'In Verwendung'},
    'It stops every start: {problem}': {
        'en': 'It stops every start: {problem}',
        'de': 'Sie verhindert jeden Start: {problem}'},
    'Named hosts allowed': {
        'en': 'Named hosts allowed',
        'de': 'Erlaubte Hostnamen'},
    'Network ceiling': {
        'en': 'Network ceiling',
        'de': 'Netzwerkgrenze'},
    'New projects': {
        'en': 'New projects',
        'de': 'Neue Projekte'},
    'No administrator policy on this machine.': {
        'en': 'No administrator policy on this machine.',
        'de': 'Keine Richtlinie des Administrators auf diesem Rechner.'},
    'Notifications stay (seconds)': {
        'en': 'Notifications stay (seconds)',
        'de': 'Benachrichtigungen bleiben (Sekunden)'},
    'Only root can change it. What it forbids is greyed out in the app.': {
        'en': 'Only root can change it. What it forbids is greyed out in the app.',
        'de': 'Nur root kann sie ändern. Was sie verbietet, ist in der App ausgegraut.'},
    'Open questions (milliseconds)': {
        'en': 'Open questions (milliseconds)',
        'de': 'Offene Fragen (Millisekunden)'},
    'Polling': {
        'en': 'Polling',
        'de': 'Abfragen'},
    'Questions are refused after {seconds} seconds without an answer: keep the question interval short.': {
        'en': 'Questions are refused after {seconds} seconds without an answer: keep the question interval short.',
        'de': 'Fragen werden nach {seconds} Sekunden ohne Antwort abgelehnt: das Intervall für Fragen kurz halten.'},
    'Running sandboxes (milliseconds)': {
        'en': 'Running sandboxes (milliseconds)',
        'de': 'Laufende Sandboxen (Millisekunden)'},
    'Settings': {
        'en': 'Settings',
        'de': 'Einstellungen'},
    'Show…': {
        'en': 'Show…',
        'de': 'Anzeigen…'},
    "The administrator's policy stops every start: {problem}": {
        'en': "The administrator's policy stops every start: {problem}",
        'de': 'Die Richtlinie des Administrators verhindert jeden Start: {problem}'},
    'There is no administrator policy on this machine.': {
        'en': 'There is no administrator policy on this machine.',
        'de': 'Auf diesem Rechner gibt es keine Richtlinie des Administrators.'},
    'Use the checkout at': {
        'en': 'Use the checkout at',
        'de': 'Den Checkout verwenden unter'},
    'Widest answer': {
        'en': 'Widest answer',
        'de': 'Weiteste Antwort'},
    'nothing': {
        'en': 'nothing',
        'de': 'nichts'},
    ' (SSH into the host)': {
        'en': ' (SSH into the host)',
        'de': ' (SSH auf den Host)'},
    'A Podman network of its own: {name}': {
        'en': 'A Podman network of its own: {name}',
        'de': 'Ein eigenes Podman-Netzwerk: {name}'},
    'Agent program {path}{why}': {
        'en': 'Agent program {path}{why}',
        'de': 'Programm des Agenten {path}{why}'},
    'Another Podman service: {url}': {
        'en': 'Another Podman service: {url}',
        'de': 'Ein anderer Podman-Dienst: {url}'},
    'Codex login imported from {path}': {
        'en': 'Codex login imported from {path}',
        'de': 'Codex-Anmeldung importiert aus {path}'},
    'Every host matching {host}': {
        'en': 'Every host matching {host}',
        'de': 'Jeder Host, der zu {host} passt'},
    'Every login in {path} is visible to the agent': {
        'en': 'Every login in {path} is visible to the agent',
        'de': 'Jede Anmeldung in {path} ist für den Agenten sichtbar'},
    'Every port and protocol to {value}: {why}': {
        'en': 'Every port and protocol to {value}: {why}',
        'de': 'Jeder Port und jedes Protokoll zu {value}: {why}'},
    'File copied into HOME: {path}': {
        'en': 'File copied into HOME: {path}',
        'de': 'Ins HOME kopierte Datei: {path}'},
    'GPU devices ({kind})': {
        'en': 'GPU devices ({kind})',
        'de': 'GPU-Geräte ({kind})'},
    'Git token visible to the agent: {server} (the token proxy keeps it out)': {
        'en': 'Git token visible to the agent: {server} (the token proxy keeps it out)',
        'de': 'Git-Token für den Agenten sichtbar: {server} (der Token-Proxy hält ihn fern)'},
    'HOME kept in': {
        'en': 'HOME kept in',
        'de': 'HOME behalten in'},
    'Host device {path}': {
        'en': 'Host device {path}',
        'de': 'Gerät des Hosts {path}'},
    'Host variable {name}': {
        'en': 'Host variable {name}',
        'de': 'Host-Variable {name}'},
    'Host variable {name} (looks like a secret)': {
        'en': 'Host variable {name} (looks like a secret)',
        'de': 'Host-Variable {name} (sieht nach einem Geheimnis aus)'},
    'Image {image}: everything in the sandbox comes from it': {
        'en': 'Image {image}: everything in the sandbox comes from it',
        'de': 'Image {image}: alles in der Sandbox stammt daraus'},
    'Kit copied into HOME from {path} (its files run inside)': {
        'en': 'Kit copied into HOME from {path} (its files run inside)',
        'de': 'Kit aus {path} ins HOME kopiert (seine Dateien laufen darin)'},
    'Login file {path}': {
        'en': 'Login file {path}',
        'de': 'Anmeldedatei {path}'},
    'MCP server {name}': {
        'en': 'MCP server {name}',
        'de': 'MCP-Server {name}'},
    "Port {port} on this host's loopback{ssh}": {
        'en': "Port {port} on this host's loopback{ssh}",
        'de': 'Port {port} auf dem Loopback dieses Hosts{ssh}'},
    'Preset {name} (HTTPS)': {
        'en': 'Preset {name} (HTTPS)',
        'de': 'Vorgabe {name} (HTTPS)'},
    'Runtime folder {path} (read-only, runs inside)': {
        'en': 'Runtime folder {path} (read-only, runs inside)',
        'de': 'Laufzeitordner {path} (nur lesbar, läuft darin)'},
    'Secret {name} as a variable': {
        'en': 'Secret {name} as a variable',
        'de': 'Geheimnis {name} als Variable'},
    'TCP {target}: {why}': {
        'en': 'TCP {target}: {why}',
        'de': 'TCP {target}: {why}'},
    'The OpenAI and Anthropic APIs (any account: a way out for data)': {
        'en': 'The OpenAI and Anthropic APIs (any account: a way out for data)',
        'de': 'Die APIs von OpenAI und Anthropic (jedes Konto: ein Weg hinaus für Daten)'},
    "The agent's login is copied in": {
        'en': "The agent's login is copied in",
        'de': 'Die Anmeldung des Agenten wird hineinkopiert'},
    "The agent's own provider": {
        'en': "The agent's own provider",
        'de': 'Der eigene Anbieter des Agenten'},
    'Token proxy ready: tokens you add stay out of the sandbox': {
        'en': 'Token proxy ready: tokens you add stay out of the sandbox',
        'de': 'Token-Proxy bereit: hinzugefügte Tokens bleiben außerhalb der Sandbox'},
    'Unknown agent {agent}: this sandbox-ai has {list}.': {
        'en': 'Unknown agent {agent}: this sandbox-ai has {list}.',
        'de': 'Unbekannter Agent {agent}: dieses sandbox-ai hat {list}.'},
    'VNC socket in {path}: whoever reaches it sees and controls the display': {
        'en': 'VNC socket in {path}: whoever reaches it sees and controls the display',
        'de': 'VNC-Socket in {path}: wer ihn erreicht, sieht und steuert den Bildschirm'},
    'a directory every user can write': {
        'en': 'a directory every user can write',
        'de': 'ein Ordner, den jeder Benutzer beschreiben kann'},
    'a large part of the internet': {
        'en': 'a large part of the internet',
        'de': 'ein großer Teil des Internets'},
    'a path that cannot be resolved': {
        'en': 'a path that cannot be resolved',
        'de': 'ein Pfad, der sich nicht auflösen lässt'},
    'a public address': {
        'en': 'a public address',
        'de': 'eine öffentliche Adresse'},
    'a system directory': {
        'en': 'a system directory',
        'de': 'ein Systemordner'},
    'an internal network (LAN, VPN, containers)': {
        'en': 'an internal network (LAN, VPN, containers)',
        'de': 'ein internes Netz (LAN, VPN, Container)'},
    'contains {path} (settings, keys or programs of the host)': {
        'en': 'contains {path} (settings, keys or programs of the host)',
        'de': 'enthält {path} (Einstellungen, Schlüssel oder Programme des Hosts)'},
    'every destination': {
        'en': 'every destination',
        'de': 'jedes Ziel'},
    'holds {name}: keys or settings': {
        'en': 'holds {name}: keys or settings',
        'de': 'enthält {name}: Schlüssel oder Einstellungen'},
    'not an address': {
        'en': 'not an address',
        'de': 'keine Adresse'},
    'settings, keys or programs of the host ({path})': {
        'en': 'settings, keys or programs of the host ({path})',
        'de': 'Einstellungen, Schlüssel oder Programme des Hosts ({path})'},
    'the whole file system': {
        'en': 'the whole file system',
        'de': 'das ganze Dateisystem'},
    "this host's loopback": {
        'en': "this host's loopback",
        'de': 'das Loopback dieses Hosts'},
    'your HOME: keys, shell start files, every other project': {
        'en': 'your HOME: keys, shell start files, every other project',
        'de': 'Ihr HOME: Schlüssel, Startdateien der Shell, jedes andere Projekt'},
    '{host} (HTTPS)': {
        'en': '{host} (HTTPS)',
        'de': '{host} (HTTPS)'},
    '{how} reaches the network: {hosts}': {
        'en': '{how} reaches the network: {hosts}',
        'de': '{how} erreicht das Netzwerk: {hosts}'},
    '{how} runs {command} in a sandbox of its own': {
        'en': '{how} runs {command} in a sandbox of its own',
        'de': '{how} führt {command} in einer eigenen Sandbox aus'},
    '{how}: {path} ({mode})': {
        'en': '{how}: {path} ({mode})',
        'de': '{how}: {path} ({mode})'},
    "A variant of an agent's image with its own extension: sandbox-ai images new": {
        'en': "A variant of an agent's image with its own extension: sandbox-ai images new",
        'de': 'Eine Variante des Images eines Agenten mit eigener Erweiterung: sandbox-ai images new'},
    'Add a file to the image': {
        'en': 'Add a file to the image',
        'de': 'Eine Datei ins Image aufnehmen'},
    'Add file…': {
        'en': 'Add file…',
        'de': 'Datei hinzufügen…'},
    'Add it to the list (saved with Save)': {
        'en': 'Add it to the list (saved with Save)',
        'de': 'Zur Liste hinzufügen (gespeichert mit Speichern)'},
    'Ask Podman again': {
        'en': 'Ask Podman again',
        'de': 'Podman erneut fragen'},
    'Build': {
        'en': 'Build',
        'de': 'Bauen'},
    'Build in a terminal window': {
        'en': 'Build in a terminal window',
        'de': 'In einem Terminalfenster bauen'},
    'Build in the app or in a window, only the extension layer, or every image': {
        'en': 'Build in the app or in a window, only the extension layer, or every image',
        'de': 'Im Terminal der App oder in einem Fenster bauen, nur die Erweiterungsschicht oder alle Images'},
    "Build in the app's terminal": {
        'en': "Build in the app's terminal",
        'de': 'Im Terminal der App bauen'},
    'Build only the extension layer': {
        'en': 'Build only the extension layer',
        'de': 'Nur die Erweiterungsschicht bauen'},
    'Cannot read the extension: {error}': {
        'en': 'Cannot read the extension: {error}',
        'de': 'Die Erweiterung lässt sich nicht lesen: {error}'},
    'Containerfile fragment': {
        'en': 'Containerfile fragment',
        'de': 'Containerfile-Fragment'},
    'Copy a host file into the extension (from a folder no sandbox can write)': {
        'en': 'Copy a host file into the extension (from a folder no sandbox can write)',
        'de': 'Eine Datei des Hosts in die Erweiterung kopieren (aus einem Ordner, den keine Sandbox beschreiben kann)'},
    'Create': {
        'en': 'Create',
        'de': 'Anlegen'},
    'Creating the variant {name}…': {
        'en': 'Creating the variant {name}…',
        'de': 'Die Variante {name} wird angelegt…'},
    'Debian package, e.g. build-essential or jq=1.6-2.1': {
        'en': 'Debian package, e.g. build-essential or jq=1.6-2.1',
        'de': 'Debian-Paket, z. B. build-essential oder jq=1.6-2.1'},
    'Debian packages': {
        'en': 'Debian packages',
        'de': 'Debian-Pakete'},
    'Delete the variant {name} and its image {image}? Projects that start it with --image-variant stop working.': {
        'en': 'Delete the variant {name} and its image {image}? Projects that start it with --image-variant stop working.',
        'de': 'Die Variante {name} und ihr Image {image} löschen? Projekte, die sie mit --image-variant starten, funktionieren dann nicht mehr.'},
    'Delete variant': {
        'en': 'Delete variant',
        'de': 'Variante löschen'},
    'Delete variant…': {
        'en': 'Delete variant…',
        'de': 'Variante löschen…'},
    'Deleting the variant {name}…': {
        'en': 'Deleting the variant {name}…',
        'de': 'Die Variante {name} wird gelöscht…'},
    'Discard': {
        'en': 'Discard',
        'de': 'Verwerfen'},
    'Discard the changes to the extension of {name}?': {
        'en': 'Discard the changes to the extension of {name}?',
        'de': 'Die Änderungen an der Erweiterung von {name} verwerfen?'},
    'Drop the changes not saved yet': {
        'en': 'Drop the changes not saved yet',
        'de': 'Die noch nicht gespeicherten Änderungen verwerfen'},
    'Extension': {
        'en': 'Extension',
        'de': 'Erweiterung'},
    'Extension of the {agent} image': {
        'en': 'Extension of the {agent} image',
        'de': 'Erweiterung des Images von {agent}'},
    'File to copy into the image': {
        'en': 'File to copy into the image',
        'de': 'Datei, die ins Image kopiert wird'},
    'Final Containerfile': {
        'en': 'Final Containerfile',
        'de': 'Fertiges Containerfile'},
    'Image variant {name}: built from your extension of the image': {
        'en': 'Image variant {name}: built from your extension of the image',
        'de': 'Image-Variante {name}: aus Ihrer Erweiterung des Images gebaut'},
    'Images and variants': {
        'en': 'Images and variants',
        'de': 'Images und Varianten'},
    'Installed packages': {
        'en': 'Installed packages',
        'de': 'Installierte Pakete'},
    "Lower-case letters, digits, '.', '_' and '-', up to 30 characters; not an agent's name.": {
        'en': "Lower-case letters, digits, '.', '_' and '-', up to 30 characters; not an agent's name.",
        'de': "Kleinbuchstaben, Ziffern, '.', '_' und '-', bis zu 30 Zeichen; nicht der Name eines Agenten."},
    'New variant': {
        'en': 'New variant',
        'de': 'Neue Variante'},
    'New variant…': {
        'en': 'New variant…',
        'de': 'Neue Variante…'},
    'No findings.': {
        'en': 'No findings.',
        'de': 'Keine Befunde.'},
    'Other builds': {
        'en': 'Other builds',
        'de': 'Weitere Builds'},
    'Path in the image': {
        'en': 'Path in the image',
        'de': 'Pfad im Image'},
    'Python tool, e.g. ruff or ruff==0.6.9': {
        'en': 'Python tool, e.g. ruff or ruff==0.6.9',
        'de': 'Python-Werkzeug, z. B. ruff oder ruff==0.6.9'},
    'Python tools': {
        'en': 'Python tools',
        'de': 'Python-Werkzeuge'},
    'RUN, ENV, COPY files/NAME … lines, added after the packages': {
        'en': 'RUN, ENV, COPY files/NAME … lines, added after the packages',
        'de': 'RUN-, ENV-, COPY-files/NAME-… Zeilen, nach den Paketen angefügt'},
    'Reading the extension of {name}…': {
        'en': 'Reading the extension of {name}…',
        'de': 'Die Erweiterung von {name} wird gelesen…'},
    'Remove': {
        'en': 'Remove',
        'de': 'Entfernen'},
    'Remove the selected packages from the list': {
        'en': 'Remove the selected packages from the list',
        'de': 'Die gewählten Pakete aus der Liste entfernen'},
    'Remove the selected variant and its image: sandbox-ai images remove': {
        'en': 'Remove the selected variant and its image: sandbox-ai images remove',
        'de': 'Die gewählte Variante und ihr Image entfernen: sandbox-ai images remove'},
    'Revert': {
        'en': 'Revert',
        'de': 'Zurücksetzen'},
    'Save': {
        'en': 'Save',
        'de': 'Speichern'},
    'Save the extension first: a build uses what is saved.': {
        'en': 'Save the extension first: a build uses what is saved.',
        'de': 'Erst die Erweiterung speichern: ein Build nimmt, was gespeichert ist.'},
    'Save the extension through sandbox-ai images extend': {
        'en': 'Save the extension through sandbox-ai images extend',
        'de': 'Die Erweiterung über sandbox-ai images extend speichern'},
    'Saved. Build it to use it: {command}': {
        'en': 'Saved. Build it to use it: {command}',
        'de': 'Gespeichert. Zum Verwenden bauen: {command}'},
    'Saving the extension of {name}…': {
        'en': 'Saving the extension of {name}…',
        'de': 'Die Erweiterung von {name} wird gespeichert…'},
    'Select an image or a variant to edit its extension.': {
        'en': 'Select an image or a variant to edit its extension.',
        'de': 'Ein Image oder eine Variante wählen, um die Erweiterung zu bearbeiten.'},
    "Start from the agent's extension": {
        'en': "Start from the agent's extension",
        'de': 'Mit der Erweiterung des Agenten beginnen'},
    'Take the selected file out of the image': {
        'en': 'Take the selected file out of the image',
        'de': 'Die gewählte Datei aus dem Image nehmen'},
    'The path in the image is absolute, the mode an octal number such as 0644.': {
        'en': 'The path in the image is absolute, the mode an octal number such as 0644.',
        'de': 'Der Pfad im Image ist absolut, der Modus eine Oktalzahl wie 0644.'},
    'This sandbox-ai cannot extend images yet; update it (sandbox-ai update).': {
        'en': 'This sandbox-ai cannot extend images yet; update it (sandbox-ai update).',
        'de': 'Dieses sandbox-ai kann Images noch nicht erweitern; bitte aktualisieren (sandbox-ai update).'},
    'Variant {name} of {agent}': {
        'en': 'Variant {name} of {agent}',
        'de': 'Variante {name} von {agent}'},
    'changed: rebuild': {
        'en': 'changed: rebuild',
        'de': 'geändert: neu bauen'},
    'current': {
        'en': 'current',
        'de': 'aktuell'},
    'e.g. gpu or web': {
        'en': 'e.g. gpu or web',
        'de': 'z. B. gpu oder web'},
    'error': {
        'en': 'error',
        'de': 'Fehler'},
    'errors must be fixed before saving': {
        'en': 'errors must be fixed before saving',
        'de': 'Fehler vor dem Speichern beheben'},
    'has errors': {
        'en': 'has errors',
        'de': 'fehlerhaft'},
    'line {line}': {
        'en': 'line {line}',
        'de': 'Zeile {line}'},
    'new': {
        'en': 'new',
        'de': 'neu'},
    'new base: rebuild': {
        'en': 'new base: rebuild',
        'de': 'neue Basis: neu bauen'},
    'no extension': {
        'en': 'no extension',
        'de': 'keine Erweiterung'},
    'not saved': {
        'en': 'not saved',
        'de': 'nicht gespeichert'},
    'npm package, e.g. typescript or @scope/name@1.2': {
        'en': 'npm package, e.g. typescript or @scope/name@1.2',
        'de': 'npm-Paket, z. B. typescript oder @scope/name@1.2'},
    'npm packages': {
        'en': 'npm packages',
        'de': 'npm-Pakete'},
    'sandbox-ai build: the base image, then the extension layer (where commands run is set on the System page)': {
        'en': 'sandbox-ai build: the base image, then the extension layer (where commands run is set on the System page)',
        'de': 'sandbox-ai build: das Basis-Image, dann die Erweiterungsschicht (wo Befehle laufen, steht auf der Seite System)'},
    'sandbox-ai did not create the variant {name}.': {
        'en': 'sandbox-ai did not create the variant {name}.',
        'de': 'sandbox-ai hat die Variante {name} nicht angelegt.'},
    'sandbox-ai did not delete the variant {name}.': {
        'en': 'sandbox-ai did not delete the variant {name}.',
        'de': 'sandbox-ai hat die Variante {name} nicht gelöscht.'},
    'sandbox-ai did not save the extension.': {
        'en': 'sandbox-ai did not save the extension.',
        'de': 'sandbox-ai hat die Erweiterung nicht gespeichert.'},
    'search the list': {
        'en': 'search the list',
        'de': 'Liste durchsuchen'},
    'warning': {
        'en': 'warning',
        'de': 'Warnung'},
    '{count} to rebuild for their extension': {
        'en': '{count} to rebuild for their extension',
        'de': '{count} für ihre Erweiterung neu zu bauen'},
    '{errors} error(s), {warnings} warning(s)': {
        'en': '{errors} error(s), {warnings} warning(s)',
        'de': '{errors} Fehler, {warnings} Warnung(en)'},
    '(built-in)': {
        'en': '(built-in)',
        'de': '(eingebaut)'},
    '(empty)': {
        'en': '(empty)',
        'de': '(leer)'},
    '({count} of the current project)': {
        'en': '({count} of the current project)',
        'de': '({count} des aktuellen Projekts)'},
    '.sandbox-ai.json cannot be read': {
        'en': '.sandbox-ai.json cannot be read',
        'de': '.sandbox-ai.json ist nicht lesbar'},
    '.sandbox-ai.json changed since you trusted it': {
        'en': '.sandbox-ai.json changed since you trusted it',
        'de': '.sandbox-ai.json hat sich seit dem Vertrauen geändert'},
    '.sandbox-ai.json not trusted yet': {
        'en': '.sandbox-ai.json not trusted yet',
        'de': '.sandbox-ai.json noch nicht vertraut'},
    '.sandbox-ai.json trusted': {
        'en': '.sandbox-ai.json trusted',
        'de': '.sandbox-ai.json vertraut'},
    "A Claude plugin marketplace from GitHub (OWNER/REPO) or a Git server (https://…). Sandboxes get only sandbox-ai's own marketplace as a folder; sandbox-ai refuses others and says why.": {
        'en': "A Claude plugin marketplace from GitHub (OWNER/REPO) or a Git server (https://…). Sandboxes get only sandbox-ai's own marketplace as a folder; sandbox-ai refuses others and says why.",
        'de': 'Ein Plugin-Marktplatz für Claude von GitHub (OWNER/REPO) oder einem Git-Server (https://…). Als Ordner bekommen Sandboxen nur den eigenen Marktplatz von sandbox-ai; andere lehnt sandbox-ai mit Begründung ab.'},
    'A kit is a folder per agent with skills, agents, commands, prompts, a memory template and plugin declarations; a project uses it with --kit NAME.': {
        'en': 'A kit is a folder per agent with skills, agents, commands, prompts, a memory template and plugin declarations; a project uses it with --kit NAME.',
        'de': 'Ein Kit ist ein Ordner je Agent mit Skills, Agenten, Befehlen, Prompts, einer Gedächtnis-Vorlage und Plugin-Angaben; ein Projekt nutzt es mit --kit NAME.'},
    'A sandbox of the current project': {
        'en': 'A sandbox of the current project',
        'de': 'Eine Sandbox des aktuellen Projekts'},
    "Add --kit NAME to the project's .sandbox-ai.json: shown first, then written and trusted": {
        'en': "Add --kit NAME to the project's .sandbox-ai.json: shown first, then written and trusted",
        'de': 'Fügt --kit NAME in die .sandbox-ai.json des Projekts ein: erst gezeigt, dann geschrieben und vertraut'},
    'Add marketplace': {
        'en': 'Add marketplace',
        'de': 'Marktplatz hinzufügen'},
    'Add marketplace…': {
        'en': 'Add marketplace…',
        'de': 'Marktplatz hinzufügen…'},
    'Add plugin': {
        'en': 'Add plugin',
        'de': 'Plugin hinzufügen'},
    'Add plugin…': {
        'en': 'Add plugin…',
        'de': 'Plugin hinzufügen…'},
    'Add skill': {
        'en': 'Add skill',
        'de': 'Skill hinzufügen'},
    'Add skill from a folder…': {
        'en': 'Add skill from a folder…',
        'de': 'Skill aus einem Ordner…'},
    'Add to a kit…': {
        'en': 'Add to a kit…',
        'de': 'Zu einem Kit hinzufügen…'},
    'Agents': {
        'en': 'Agents',
        'de': 'Agenten'},
    'Allow more destinations until the sandbox ends (sandbox-ai net allow)': {
        'en': 'Allow more destinations until the sandbox ends (sandbox-ai net allow)',
        'de': 'Weitere Ziele erlauben, bis die Sandbox endet (sandbox-ai net allow)'},
    'Cannot list the kits: {error}': {
        'en': 'Cannot list the kits: {error}',
        'de': 'Die Kits lassen sich nicht auflisten: {error}'},
    'Changes not applied': {
        'en': 'Changes not applied',
        'de': 'Nicht übernommene Änderungen'},
    'Choose a project first (at the top of the window).': {
        'en': 'Choose a project first (at the top of the window).',
        'de': 'Zuerst ein Projekt wählen (oben im Fenster).'},
    'Choose a project folder to work in': {
        'en': 'Choose a project folder to work in',
        'de': 'Einen Projektordner zum Arbeiten wählen'},
    "Claude: NAME@MARKETPLACE (a plugin of sandbox-ai's own marketplace needs no source); OpenCode: an npm package name.": {
        'en': "Claude: NAME@MARKETPLACE (a plugin of sandbox-ai's own marketplace needs no source); OpenCode: an npm package name.",
        'de': 'Claude: NAME@MARKTPLATZ (ein Plugin aus dem eigenen Marktplatz von sandbox-ai braucht keine Quelle); OpenCode: ein npm-Paketname.'},
    'Comes from': {
        'en': 'Comes from',
        'de': 'Herkunft'},
    'Commands': {
        'en': 'Commands',
        'de': 'Befehle'},
    'Declare the selected plugin in one of your kits (sandbox-ai kits add-plugin)': {
        'en': 'Declare the selected plugin in one of your kits (sandbox-ai kits add-plugin)',
        'de': 'Das gewählte Plugin in einem Ihrer Kits eintragen (sandbox-ai kits add-plugin)'},
    'Delete kit…': {
        'en': 'Delete kit…',
        'de': 'Kit löschen…'},
    'Delete the kit {name} with everything in it ({path})? Projects that use it can no longer start until you remove --kit {name}.': {
        'en': 'Delete the kit {name} with everything in it ({path})? Projects that use it can no longer start until you remove --kit {name}.',
        'de': 'Das Kit {name} mit allem darin löschen ({path})? Projekte, die es nutzen, starten erst wieder, wenn --kit {name} entfernt ist.'},
    'Description': {
        'en': 'Description',
        'de': 'Beschreibung'},
    'Drop the changes': {
        'en': 'Drop the changes',
        'de': 'Änderungen verwerfen'},
    'End the selected sandbox: a session (its state is kept), an MCP server, or a run (sandbox-ai net stop)': {
        'en': 'End the selected sandbox: a session (its state is kept), an MCP server, or a run (sandbox-ai net stop)',
        'de': 'Die gewählte Sandbox beenden: eine Sitzung (ihr Zustand bleibt), einen MCP-Server oder einen Lauf (sandbox-ai net stop)'},
    'Failed (exit status {status}).': {
        'en': 'Failed (exit status {status}).',
        'de': 'Fehlgeschlagen (Exit-Status {status}).'},
    'Folder…': {
        'en': 'Folder…',
        'de': 'Ordner…'},
    'For': {
        'en': 'For',
        'de': 'Für'},
    'In kits': {
        'en': 'In kits',
        'de': 'In Kits'},
    'Insight': {
        'en': 'Insight',
        'de': 'Einblick'},
    'Install plugins': {
        'en': 'Install plugins',
        'de': 'Plugins installieren'},
    'Installed': {
        'en': 'Installed',
        'de': 'Installiert'},
    'It does not run.': {
        'en': 'It does not run.',
        'de': 'Sie läuft nicht.'},
    'It has not registered with sandbox-ai (yet): starting, or started by an older sandbox-ai. End it in its terminal.': {
        'en': 'It has not registered with sandbox-ai (yet): starting, or started by an older sandbox-ai. End it in its terminal.',
        'de': 'Sie hat sich (noch) nicht bei sandbox-ai angemeldet: sie startet gerade oder kam von einer älteren sandbox-ai. Beenden Sie sie in ihrem Terminal.'},
    'Keep them': {
        'en': 'Keep them',
        'de': 'Behalten'},
    'Kit': {
        'en': 'Kit',
        'de': 'Kit'},
    'Kits': {
        'en': 'Kits',
        'de': 'Kits'},
    'Kits & plugins': {
        'en': 'Kits & plugins',
        'de': 'Kits & Plugins'},
    'Kits (--kit)': {
        'en': 'Kits (--kit)',
        'de': 'Kits (--kit)'},
    'Known from: {list}': {
        'en': 'Known from: {list}',
        'de': 'Bekannt aus: {list}'},
    'Manage': {
        'en': 'Manage',
        'de': 'Verwalten'},
    'Marketplace folder': {
        'en': 'Marketplace folder',
        'de': 'Marktplatz-Ordner'},
    'Marketplace source': {
        'en': 'Marketplace source',
        'de': 'Marktplatz-Quelle'},
    'Marketplaces: {count}': {
        'en': 'Marketplaces: {count}',
        'de': 'Marktplätze: {count}'},
    'Memory template: {name}': {
        'en': 'Memory template: {name}',
        'de': 'Gedächtnis-Vorlage: {name}'},
    'Name (a-z, 0-9, -)': {
        'en': 'Name (a-z, 0-9, -)',
        'de': 'Name (a-z, 0-9, -)'},
    'New kit': {
        'en': 'New kit',
        'de': 'Neues Kit'},
    'New kit…': {
        'en': 'New kit…',
        'de': 'Neues Kit…'},
    'New project (wizard)…': {
        'en': 'New project (wizard)…',
        'de': 'Neues Projekt (Assistent)…'},
    'New skill': {
        'en': 'New skill',
        'de': 'Neuer Skill'},
    'New skill…': {
        'en': 'New skill…',
        'de': 'Neuer Skill…'},
    'No .sandbox-ai.json in this project: the options below are all there is.': {
        'en': 'No .sandbox-ai.json in this project: the options below are all there is.',
        'de': 'Keine .sandbox-ai.json in diesem Projekt: es gelten nur die Optionen unten.'},
    'No kit of this name: the launcher refuses to start': {
        'en': 'No kit of this name: the launcher refuses to start',
        'de': 'Kein Kit mit diesem Namen: der Launcher verweigert den Start'},
    'No kits yet: create one on the Kits & plugins page.': {
        'en': 'No kits yet: create one on the Kits & plugins page.',
        'de': 'Noch keine Kits: legen Sie eines auf der Seite Kits & Plugins an.'},
    'No project': {
        'en': 'No project',
        'de': 'Kein Projekt'},
    'No project chosen': {
        'en': 'No project chosen',
        'de': 'Kein Projekt gewählt'},
    'Not for {agent}: only for {agents}': {
        'en': 'Not for {agent}: only for {agents}',
        'de': 'Nicht für {agent}: nur für {agents}'},
    'Not registered': {
        'en': 'Not registered',
        'de': 'Nicht angemeldet'},
    'It runs in the foreground of its own terminal (started with --no-tmux or without a terminal): there is nothing to attach to.': {
        'en': 'It runs in the foreground of its own terminal (started with --no-tmux or without a terminal): there is nothing to attach to.',
        'de': 'Sie läuft im Vordergrund ihres eigenen Terminals (mit --no-tmux oder ohne Terminal gestartet): Es gibt nichts zum Verbinden.'},
    'Not while the session is {state}.': {
        'en': 'Not while the session is {state}.',
        'de': 'Nicht, solange die Sitzung {state} ist.'},
    'OWNER/REPO or https://git.example.org/plugins.git': {
        'en': 'OWNER/REPO or https://git.example.org/plugins.git',
        'de': 'OWNER/REPO oder https://git.example.org/plugins.git'},
    'Only for sessions.': {
        'en': 'Only for sessions.',
        'de': 'Nur für Sitzungen.'},
    'Only the current project': {
        'en': 'Only the current project',
        'de': 'Nur das aktuelle Projekt'},
    'Open folder…': {
        'en': 'Open folder…',
        'de': 'Ordner öffnen…'},
    'Plugin': {
        'en': 'Plugin',
        'de': 'Plugin'},
    'Plugin store {path}: {list}': {
        'en': 'Plugin store {path}: {list}',
        'de': 'Plugin-Speicher {path}: {list}'},
    'Plugins: {count}': {
        'en': 'Plugins: {count}',
        'de': 'Plugins: {count}'},
    'Project {path}': {
        'en': 'Project {path}',
        'de': 'Projekt {path}'},
    'Prompts': {
        'en': 'Prompts',
        'de': 'Prompts'},
    'Read again now (it is read every few seconds anyway)': {
        'en': 'Read again now (it is read every few seconds anyway)',
        'de': 'Jetzt neu lesen (es wird ohnehin alle paar Sekunden gelesen)'},
    'Reading the kits (sandbox-ai kits list)…': {
        'en': 'Reading the kits (sandbox-ai kits list)…',
        'de': 'Die Kits werden gelesen (sandbox-ai kits list)…'},
    'Reading the kits…': {
        'en': 'Reading the kits…',
        'de': 'Die Kits werden gelesen…'},
    'Remove from the kit': {
        'en': 'Remove from the kit',
        'de': 'Aus dem Kit entfernen'},
    'Remove…': {
        'en': 'Remove…',
        'de': 'Entfernen…'},
    'Replaced by your kit of the same name for: {list}': {
        'en': 'Replaced by your kit of the same name for: {list}',
        'de': 'Ersetzt durch Ihr gleichnamiges Kit für: {list}'},
    'Review and trust…': {
        'en': 'Review and trust…',
        'de': 'Prüfen und vertrauen…'},
    'Run {run}': {
        'en': 'Run {run}',
        'de': 'Lauf {run}'},
    'Running': {
        'en': 'Running',
        'de': 'Laufend'},
    'Sandboxes & sessions': {
        'en': 'Sandboxes & sessions',
        'de': 'Sandboxen & Sitzungen'},
    'Select a kit first.': {
        'en': 'Select a kit first.',
        'de': 'Zuerst ein Kit wählen.'},
    'Select a kit to see what it brings.': {
        'en': 'Select a kit to see what it brings.',
        'de': 'Ein Kit wählen, um zu sehen, was es mitbringt.'},
    'Select a plugin of the marketplace first.': {
        'en': 'Select a plugin of the marketplace first.',
        'de': 'Zuerst ein Plugin des Marktplatzes wählen.'},
    'Select a row first.': {
        'en': 'Select a row first.',
        'de': 'Zuerst eine Zeile wählen.'},
    'Select a row: the buttons below act on it.': {
        'en': 'Select a row: the buttons below act on it.',
        'de': 'Eine Zeile wählen: die Schaltflächen unten wirken auf sie.'},
    "Show the project's .sandbox-ai.json and trust exactly that content": {
        'en': "Show the project's .sandbox-ai.json and trust exactly that content",
        'de': 'Die .sandbox-ai.json des Projekts zeigen und genau diesem Inhalt vertrauen'},
    'Skill folder (with SKILL.md)': {
        'en': 'Skill folder (with SKILL.md)',
        'de': 'Skill-Ordner (mit SKILL.md)'},
    'Source': {
        'en': 'Source',
        'de': 'Quelle'},
    'Started': {
        'en': 'Started',
        'de': 'Gestartet'},
    'Stop {what}? Agents that use it lose it until it is started again.': {
        'en': 'Stop {what}? Agents that use it lose it until it is started again.',
        'de': '{what} stoppen? Agenten, die ihn nutzen, verlieren ihn, bis er wieder gestartet wird.'},
    'Stop {what}? It ends; its HOME is kept and Resume starts it again.': {
        'en': 'Stop {what}? It ends; its HOME is kept and Resume starts it again.',
        'de': '{what} stoppen? Sie endet; ihr HOME bleibt, und Fortsetzen startet sie wieder.'},
    'Stop {what}? Its agent ends; what it did not save is lost.': {
        'en': 'Stop {what}? Its agent ends; what it did not save is lost.',
        'de': '{what} stoppen? Sein Agent endet; was er nicht gespeichert hat, geht verloren.'},
    'Stop…': {
        'en': 'Stop…',
        'de': 'Stoppen…'},
    'Switch the project: the Start page, the Board, Connections, Statistics, Files and Approvals follow it': {
        'en': 'Switch the project: the Start page, the Board, Connections, Statistics, Files and Approvals follow it',
        'de': 'Das Projekt wechseln: Start, Tafel, Verbindungen, Statistik, Dateien und Freigaben folgen ihm'},
    'Take back what was allowed during the run (sandbox-ai net revoke)': {
        'en': 'Take back what was allowed during the run (sandbox-ai net revoke)',
        'de': 'Zurücknehmen, was während des Laufs erlaubt wurde (sandbox-ai net revoke)'},
    'The board of {path} has changes that are not applied. Drop them?': {
        'en': 'The board of {path} has changes that are not applied. Drop them?',
        'de': 'Die Tafel von {path} hat nicht übernommene Änderungen. Verwerfen?'},
    "The connection proxy's log of its project": {
        'en': "The connection proxy's log of its project",
        'de': 'Das Log des Verbindungs-Proxys seines Projekts'},
    'The current project uses it already.': {
        'en': 'The current project uses it already.',
        'de': 'Das aktuelle Projekt nutzt es schon.'},
    'The default kit is edited in its folder: {path}': {
        'en': 'The default kit is edited in its folder: {path}',
        'de': 'Das Standard-Kit wird in seinem Ordner bearbeitet: {path}'},
    "The project's .sandbox-ai.json cannot be read.": {
        'en': "The project's .sandbox-ai.json cannot be read.",
        'de': 'Die .sandbox-ai.json des Projekts ist nicht lesbar.'},
    "The project's .sandbox-ai.json cannot be read: {error}": {
        'en': "The project's .sandbox-ai.json cannot be read: {error}",
        'de': 'Die .sandbox-ai.json des Projekts ist nicht lesbar: {error}'},
    "The project's .sandbox-ai.json cannot be used: {problems}": {
        'en': "The project's .sandbox-ai.json cannot be used: {problems}",
        'de': 'Die .sandbox-ai.json des Projekts ist nicht verwendbar: {problems}'},
    "The project's .sandbox-ai.json changed since you trusted it: the launcher refuses to start until you review it.": {
        'en': "The project's .sandbox-ai.json changed since you trusted it: the launcher refuses to start until you review it.",
        'de': 'Die .sandbox-ai.json des Projekts hat sich seit dem Vertrauen geändert: der Launcher startet erst, wenn Sie sie geprüft haben.'},
    "The project's .sandbox-ai.json is not trusted yet: the launcher refuses to start until you review it.": {
        'en': "The project's .sandbox-ai.json is not trusted yet: the launcher refuses to start until you review it.",
        'de': 'Der .sandbox-ai.json des Projekts wird noch nicht vertraut: der Launcher startet erst, wenn Sie sie geprüft haben.'},
    "The project's trusted .sandbox-ai.json is applied by the launcher on top of these options.": {
        'en': "The project's trusted .sandbox-ai.json is applied by the launcher on top of these options.",
        'de': 'Die vertraute .sandbox-ai.json des Projekts wendet der Launcher zusätzlich zu diesen Optionen an.'},
    'The session does not run.': {
        'en': 'The session does not run.',
        'de': 'Die Sitzung läuft nicht.'},
    'This kit is part of every sandbox already.': {
        'en': 'This kit is part of every sandbox already.',
        'de': 'Dieses Kit gehört schon zu jeder Sandbox.'},
    'This project': {
        'en': 'This project',
        'de': 'Dieses Projekt'},
    'This sandbox-ai cannot list its kits (sandbox-ai kits is missing): type their names below.': {
        'en': 'This sandbox-ai cannot list its kits (sandbox-ai kits is missing): type their names below.',
        'de': 'Diese sandbox-ai kann ihre Kits nicht auflisten (sandbox-ai kits fehlt): tragen Sie die Namen unten ein.'},
    'This sandbox-ai cannot stop a run from outside (sandbox-ai net stop is missing; update it). End the run in its terminal.': {
        'en': 'This sandbox-ai cannot stop a run from outside (sandbox-ai net stop is missing; update it). End the run in its terminal.',
        'de': 'Diese sandbox-ai kann einen Lauf nicht von außen stoppen (sandbox-ai net stop fehlt; aktualisieren Sie sie). Beenden Sie den Lauf in seinem Terminal.'},
    'This sandbox-ai has no kits command (sandbox-ai kits); update it with: sandbox-ai update': {
        'en': 'This sandbox-ai has no kits command (sandbox-ai kits); update it with: sandbox-ai update',
        'de': 'Diese sandbox-ai hat keinen Befehl kits (sandbox-ai kits); aktualisieren mit: sandbox-ai update'},
    'Update plugins': {
        'en': 'Update plugins',
        'de': 'Plugins aktualisieren'},
    'Use in the current project…': {
        'en': 'Use in the current project…',
        'de': 'Im aktuellen Projekt nutzen…'},
    'Use the kit {name}': {
        'en': 'Use the kit {name}',
        'de': 'Das Kit {name} nutzen'},
    'What it brings': {
        'en': 'What it brings',
        'de': 'Was es mitbringt'},
    'What was allowed since the start (sandbox-ai net show)': {
        'en': 'What was allowed since the start (sandbox-ai net show)',
        'de': 'Was seit dem Start erlaubt wurde (sandbox-ai net show)'},
    'While it runs': {
        'en': 'While it runs',
        'de': 'Während sie läuft'},
    'Writes SKILL.md from a template; describe when the agent should use it, then fill it in its folder.': {
        'en': 'Writes SKILL.md from a template; describe when the agent should use it, then fill it in its folder.',
        'de': 'Schreibt SKILL.md aus einer Vorlage; beschreiben Sie, wann der Agent ihn nutzen soll, und füllen Sie ihn dann in seinem Ordner.'},
    'a running sandbox': {
        'en': 'a running sandbox',
        'de': 'einer laufenden Sandbox'},
    'a session': {
        'en': 'a session',
        'de': 'einer Sitzung'},
    'an earlier run': {
        'en': 'an earlier run',
        'de': 'einem früheren Lauf'},
    'answers remembered always': {
        'en': 'answers remembered always',
        'de': 'dauerhaft gemerkten Antworten'},
    'built-in': {
        'en': 'built-in',
        'de': 'eingebaut'},
    'damaged': {
        'en': 'damaged',
        'de': 'beschädigt'},
    'installed': {
        'en': 'installed',
        'de': 'installiert'},
    'my-kit': {
        'en': 'my-kit',
        'de': 'mein-kit'},
    'my-marketplace': {
        'en': 'my-marketplace',
        'de': 'mein-marktplatz'},
    'no': {
        'en': 'no',
        'de': 'nein'},
    'no .sandbox-ai.json yet': {
        'en': 'no .sandbox-ai.json yet',
        'de': 'noch keine .sandbox-ai.json'},
    'not all could be read: {problems}': {
        'en': 'not all could be read: {problems}',
        'de': 'nicht alles lesbar: {problems}'},
    'not in the run registry': {
        'en': 'not in the run registry',
        'de': 'nicht im Laufregister'},
    'not installed': {
        'en': 'not installed',
        'de': 'nicht installiert'},
    'only for a new marketplace: OWNER/REPO or https://…': {
        'en': 'only for a new marketplace: OWNER/REPO or https://…',
        'de': 'nur für einen neuen Marktplatz: OWNER/REPO oder https://…'},
    'opened in this app': {
        'en': 'opened in this app',
        'de': 'in dieser App geöffnet'},
    'paused': {
        'en': 'paused',
        'de': 'pausiert'},
    'plugin {name} ({agent})': {
        'en': 'plugin {name} ({agent})',
        'de': 'Plugin {name} ({agent})'},
    'review-checklist': {
        'en': 'review-checklist',
        'de': 'review-checkliste'},
    'run {name}': {
        'en': 'run {name}',
        'de': 'Lauf {name}'},
    'running': {
        'en': 'running',
        'de': 'läuft'},
    'sandbox-ai kits new: a kit of your own in ~/.local/share/sandbox-ai/kits': {
        'en': 'sandbox-ai kits new: a kit of your own in ~/.local/share/sandbox-ai/kits',
        'de': 'sandbox-ai kits new: ein eigenes Kit in ~/.local/share/sandbox-ai/kits'},
    'sandbox-ai plugins claude --update, in a terminal': {
        'en': 'sandbox-ai plugins claude --update, in a terminal',
        'de': 'sandbox-ai plugins claude --update, in einem Terminal'},
    'sandbox-ai plugins claude, in a terminal: installs what the kits declare into the plugin store (with --kit for the built-in kits this project uses)': {
        'en': 'sandbox-ai plugins claude, in a terminal: installs what the kits declare into the plugin store (with --kit for the built-in kits this project uses)',
        'de': 'sandbox-ai plugins claude, in einem Terminal: installiert, was die Kits angeben, in den Plugin-Speicher (mit --kit für die eingebauten Kits dieses Projekts)'},
    "sandbox-ai's own kits are read-only: create a kit of your own (New kit…).": {
        'en': "sandbox-ai's own kits are read-only: create a kit of your own (New kit…).",
        'de': 'Die eigenen Kits von sandbox-ai sind schreibgeschützt: legen Sie ein eigenes Kit an (Neues Kit…).'},
    "sandbox-ai's plugin marketplace (sandbox-ai)": {
        'en': "sandbox-ai's plugin marketplace (sandbox-ai)",
        'de': 'Plugin-Marktplatz von sandbox-ai (sandbox-ai)'},
    'skill {name} ({agent})': {
        'en': 'skill {name} ({agent})',
        'de': 'Skill {name} ({agent})'},
    'starting': {
        'en': 'starting',
        'de': 'startet'},
    'the new board': {
        'en': 'the new board',
        'de': 'der neuen Tafel'},
    'trusted project configuration': {
        'en': 'trusted project configuration',
        'de': 'einer vertrauten Projektkonfiguration'},
    'yours': {
        'en': 'yours',
        'de': 'Ihres'},
    '{count} in the current project': {
        'en': '{count} in the current project',
        'de': '{count} im aktuellen Projekt'},
    '{count} kits ({user} of yours) in {path}': {
        'en': '{count} kits ({user} of yours) in {path}',
        'de': '{count} Kits ({user} eigene) in {path}'},
    '{count} running here': {
        'en': '{count} running here',
        'de': '{count} laufen hier'},
    '{count} running on this account': {
        'en': '{count} running on this account',
        'de': '{count} laufen in diesem Konto'},
    '{name} (not found)': {
        'en': '{name} (not found)',
        'de': '{name} (nicht gefunden)'},
    '{name} is not a valid name.': {
        'en': '{name} is not a valid name.',
        'de': '{name} ist kein gültiger Name.'},
    '{page} (Ctrl+{number})': {
        'en': '{page} (Ctrl+{number})',
        'de': '{page} (Strg+{number})'},
    '{path} is not a folder.': {
        'en': '{path} is not a folder.',
        'de': '{path} ist kein Ordner.'},
    '{path} uses the kit {name} already.': {
        'en': '{path} uses the kit {name} already.',
        'de': '{path} nutzt das Kit {name} schon.'},
    'open questions: {count}': {
        'en': 'open questions: {count}',
        'de': 'offene Fragen: {count}'},
    'always (sandbox-ai)': {
        'en': 'always (sandbox-ai)',
        'de': 'immer (sandbox-ai)'},
    'always (default kit)': {
        'en': 'always (default kit)',
        'de': 'immer (Standard-Kit)'},
    'Brings': {
        'en': 'Brings',
        'de': 'Bringt mit'},
    'skills: {list}': {
        'en': 'skills: {list}',
        'de': 'Skills: {list}'},
    'plugins: {list}': {
        'en': 'plugins: {list}',
        'de': 'Plugins: {list}'},
    'Skills': {
        'en': 'Skills',
        'de': 'Skills'},
    '&Settings': {
        'en': '&Settings',
        'de': '&Einstellungen'},
    'Settings…': {
        'en': 'Settings…',
        'de': 'Einstellungen…'},
    'Administrator policy…': {
        'en': 'Administrator policy…',
        'de': 'Richtlinie des Administrators…'},
    '&File': {
        'en': '&File',
        'de': '&Datei'},
    '&Go': {
        'en': '&Go',
        'de': '&Gehe zu'},
    '&Help': {
        'en': '&Help',
        'de': '&Hilfe'},
    '&View': {
        'en': '&View',
        'de': '&Ansicht'},
    'About sandbox-ai-app': {
        'en': 'About sandbox-ai-app',
        'de': 'Über sandbox-ai-app'},
    'Approvals: open questions': {
        'en': 'Approvals: open questions',
        'de': 'Freigaben: offene Fragen'},
    'Board zoom': {
        'en': 'Board zoom',
        'de': 'Zoom der Tafel'},
    'Board: inspector and findings': {
        'en': 'Board: inspector and findings',
        'de': 'Tafel: Eigenschaften und Befunde'},
    'Board: parts': {
        'en': 'Board: parts',
        'de': 'Tafel: Bausteine'},
    'Close and drop them': {
        'en': 'Close and drop them',
        'de': 'Schließen und verwerfen'},
    'Close other tabs': {
        'en': 'Close other tabs',
        'de': 'Andere Tabs schließen'},
    'Close tab': {
        'en': 'Close tab',
        'de': 'Tab schließen'},
    'Fit the board': {
        'en': 'Fit the board',
        'de': 'Tafel einpassen'},
    'Full screen': {
        'en': 'Full screen',
        'de': 'Vollbild'},
    'Full screen (F11)': {
        'en': 'Full screen (F11)',
        'de': 'Vollbild (F11)'},
    'Hide to the system tray': {
        'en': 'Hide to the system tray',
        'de': 'In den Systembereich'},
    'Hide to the system tray: sandbox-ai keeps answering there': {
        'en': 'Hide to the system tray: sandbox-ai keeps answering there',
        'de': 'In den Systembereich: sandbox-ai antwortet dort weiter'},
    'Keep the tab': {
        'en': 'Keep the tab',
        'de': 'Tab behalten'},
    'Leave full screen (F11)': {
        'en': 'Leave full screen (F11)',
        'de': 'Vollbild verlassen (F11)'},
    'Maximize': {
        'en': 'Maximize',
        'de': 'Maximieren'},
    'Minimize': {
        'en': 'Minimize',
        'de': 'Minimieren'},
    'New project…': {
        'en': 'New project…',
        'de': 'Neues Projekt…'},
    'Next tab': {
        'en': 'Next tab',
        'de': 'Nächster Tab'},
    'No project known yet': {
        'en': 'No project known yet',
        'de': 'Noch kein Projekt bekannt'},
    'Open a project, session or running sandbox in a new tab': {
        'en': 'Open a project, session or running sandbox in a new tab',
        'de': 'Ein Projekt, eine Sitzung oder eine laufende Sandbox in einem neuen Tab öffnen'},
    'Open a tab for each session or sandbox that starts': {
        'en': 'Open a tab for each session or sandbox that starts',
        'de': 'Für jede startende Sitzung oder Sandbox einen Tab öffnen'},
    'Panels': {
        'en': 'Panels',
        'de': 'Bereiche'},
    'Previous tab': {
        'en': 'Previous tab',
        'de': 'Vorheriger Tab'},
    'Projects': {
        'en': 'Projects',
        'de': 'Projekte'},
    'Quit sandbox-ai-app (Ctrl+Q)': {
        'en': 'Quit sandbox-ai-app (Ctrl+Q)',
        'de': 'sandbox-ai-app beenden (Strg+Q)'},
    'Read the README': {
        'en': 'Read the README',
        'de': 'README lesen'},
    'Reset the panels': {
        'en': 'Reset the panels',
        'de': 'Bereiche zurücksetzen'},
    'Restore': {
        'en': 'Restore',
        'de': 'Wiederherstellen'},
    'Sessions and sandboxes': {
        'en': 'Sessions and sandboxes',
        'de': 'Sitzungen und Sandboxen'},
    'Show in Sandboxes & sessions': {
        'en': 'Show in Sandboxes & sessions',
        'de': 'In Sandboxen & Sitzungen zeigen'},
    'Switch project': {
        'en': 'Switch project',
        'de': 'Projekt wechseln'},
    'The README is not installed with this copy of the app: see https://github.com/c0decave/sandbox-ai': {
        'en': 'The README is not installed with this copy of the app: see https://github.com/c0decave/sandbox-ai',
        'de': 'Die README ist mit dieser Kopie der App nicht installiert: siehe https://github.com/c0decave/sandbox-ai'},
    'The board of {path} has changes that are not applied. Close the tab and drop them?': {
        'en': 'The board of {path} has changes that are not applied. Close the tab and drop them?',
        'de': 'Die Tafel von {path} hat nicht übernommene Änderungen. Tab schließen und sie verwerfen?'},
    'Zoom in': {
        'en': 'Zoom in',
        'de': 'Vergrößern'},
    'Zoom out': {
        'en': 'Zoom out',
        'de': 'Verkleinern'},
    'sandbox-ai-app {app}\nsandbox-ai {sai} from {path}\nPySide6 {pyside}, Qt {qt}\nPython {python}': {
        'en': 'sandbox-ai-app {app}\nsandbox-ai {sai} from {path}\nPySide6 {pyside}, Qt {qt}\nPython {python}',
        'de': 'sandbox-ai-app {app}\nsandbox-ai {sai} aus {path}\nPySide6 {pyside}, Qt {qt}\nPython {python}'},
    "Drop the restored changes: the board as the project's file says": {
        'en': "Drop the restored changes: the board as the project's file says",
        'de': 'Die wiederhergestellten Änderungen verwerfen: die Tafel, wie die Datei des Projekts sie beschreibt'},
    'Restore the changes not applied': {
        'en': 'Restore the changes not applied',
        'de': 'Nicht übernommene Änderungen wiederherstellen'},
    "Restored the changes not applied when the app was last closed: Undo goes back to the project's file, Discard drops them.": {
        'en': "Restored the changes not applied when the app was last closed: Undo goes back to the project's file, Discard drops them.",
        'de': 'Die beim letzten Schließen nicht übernommenen Änderungen sind wiederhergestellt: Rückgängig führt zur Datei des Projekts zurück, Verwerfen verwirft sie.'},
    'Saved settings could not be read; the app started with defaults: {problems}': {
        'en': 'Saved settings could not be read; the app started with defaults: {problems}',
        'de': 'Gespeicherte Einstellungen waren nicht lesbar; die App startete mit Voreinstellungen: {problems}'},
    'prompt': {
        'en': 'prompt',
        'de': 'Eingabe'},
    'reply': {
        'en': 'reply',
        'de': 'Antwort'},
    'tool': {
        'en': 'tool',
        'de': 'Werkzeug'},
    'command': {
        'en': 'command',
        'de': 'Befehl'},
    'network': {
        'en': 'network',
        'de': 'Netzwerk'},
    'approval': {
        'en': 'approval',
        'de': 'Freigabe'},
    'MCP': {
        'en': 'MCP',
        'de': 'MCP'},
    'session': {
        'en': 'session',
        'de': 'Sitzung'},
    'tokens': {
        'en': 'tokens',
        'de': 'Tokens'},
    'Activity': {
        'en': 'Activity',
        'de': 'Aktivität'},
    'Kinds': {
        'en': 'Kinds',
        'de': 'Arten'},
    'Kinds ({count})': {
        'en': 'Kinds ({count})',
        'de': 'Arten ({count})'},
    'All kinds': {
        'en': 'All kinds',
        'de': 'Alle Arten'},
    'Only what the agent did': {
        'en': 'Only what the agent did',
        'de': 'Nur was der Agent tat'},
    'filter (text, path, host, command)': {
        'en': 'filter (text, path, host, command)',
        'de': 'Filter (Text, Pfad, Host, Befehl)'},
    'Export…': {
        'en': 'Export…',
        'de': 'Exportieren…'},
    'As JSON lines…': {
        'en': 'As JSON lines…',
        'de': 'Als JSON-Zeilen…'},
    'As text…': {
        'en': 'As text…',
        'de': 'Als Text…'},
    'Show': {
        'en': 'Show',
        'de': 'Zeigen'},
    'Choose an event to see everything recorded about it.': {
        'en': 'Choose an event to see everything recorded about it.',
        'de': 'Wählen Sie ein Ereignis, um alles zu sehen, was dazu aufgezeichnet ist.'},
    'Show in Connections': {
        'en': 'Show in Connections',
        'de': 'In Verbindungen zeigen'},
    'Show in Files': {
        'en': 'Show in Files',
        'de': 'In Dateien zeigen'},
    'Reading…': {
        'en': 'Reading…',
        'de': 'Wird gelesen…'},
    'Choose a network event': {
        'en': 'Choose a network event',
        'de': 'Wählen Sie ein Netzwerk-Ereignis'},
    'Choose a file event inside a workspace': {
        'en': 'Choose a file event inside a workspace',
        'de': 'Wählen Sie ein Datei-Ereignis in einem Workspace'},
    'Export the activity': {
        'en': 'Export the activity',
        'de': 'Aktivität exportieren'},
    '{count} events exported to {path}': {
        'en': '{count} events exported to {path}',
        'de': '{count} Ereignisse nach {path} exportiert'},
    'Folder: {path}': {
        'en': 'Folder: {path}',
        'de': 'Ordner: {path}'},
    'No sandbox has recorded activity yet: it is written while a sandbox with a log folder runs.': {
        'en': 'No sandbox has recorded activity yet: it is written while a sandbox with a log folder runs.',
        'de': 'Noch keine Sandbox hat Aktivität aufgezeichnet: Sie wird geschrieben, während eine Sandbox mit Log-Ordner läuft.'},
    '{count} events': {
        'en': '{count} events',
        'de': '{count} Ereignisse'},
    '{shown} of {count} events shown': {
        'en': '{shown} of {count} events shown',
        'de': '{shown} von {count} Ereignissen angezeigt'},
    'JSON lines (*.jsonl)': {
        'en': 'JSON lines (*.jsonl)',
        'de': 'JSON-Zeilen (*.jsonl)'},
    'Text (*.txt)': {
        'en': 'Text (*.txt)',
        'de': 'Text (*.txt)'},
    'Running: {run} ({workspace})': {
        'en': 'Running: {run} ({workspace})',
        'de': 'Läuft: {run} ({workspace})'},
    'Session: {name} ({status})': {
        'en': 'Session: {name} ({status})',
        'de': 'Session: {name} ({status})'},
    'Project: {name} (every run)': {
        'en': 'Project: {name} (every run)',
        'de': 'Projekt: {name} (alle Läufe)'},
    'Not exported': {
        'en': 'Not exported',
        'de': 'Nicht exportiert'},
    'Cannot write {path}: {error}': {
        'en': 'Cannot write {path}: {error}',
        'de': '{path} kann nicht geschrieben werden: {error}'},
    'This sandbox-ai has no activity timeline yet; update it with: sandbox-ai update': {
        'en': 'This sandbox-ai has no activity timeline yet; update it with: sandbox-ai update',
        'de': 'Dieses sandbox-ai hat noch keine Aktivitäts-Zeitleiste; aktualisieren Sie es mit: sandbox-ai update'},
    'a VPN profile (sandbox-ai vpn list)': {
        'en': 'a VPN profile (sandbox-ai vpn list)',
        'de': 'ein VPN-Profil (sandbox-ai vpn list)'},
    'an LLM server (sandbox-ai llm list)': {
        'en': 'an LLM server (sandbox-ai llm list)',
        'de': 'ein LLM-Server (sandbox-ai llm list)'},
    'an MCP server (sandbox-ai mcp list)': {
        'en': 'an MCP server (sandbox-ai mcp list)',
        'de': 'ein MCP-Server (sandbox-ai mcp list)'},
    'a folder': {
        'en': 'a folder',
        'de': 'ein Ordner'},
    'a GPU: nvidia, dri or rocm': {
        'en': 'a GPU: nvidia, dri or rocm',
        'de': 'eine GPU: nvidia, dri oder rocm'},
    'a value': {
        'en': 'a value',
        'de': 'ein Wert'},
    'LLM server {name}: its address and port only': {
        'en': 'LLM server {name}: its address and port only',
        'de': 'LLM-Server {name}: nur seine Adresse und sein Port'},
    'Rulesets allow, ask or deny each request and tool call ({count})': {
        'en': 'Rulesets allow, ask or deny each request and tool call ({count})',
        'de': 'Regelsätze erlauben, fragen nach oder verweigern jede Anfrage und jeden Tool-Aufruf ({count})'},
    'Approvals and rules': {
        'en': 'Approvals and rules',
        'de': 'Freigaben und Regeln'},
    'LLM server': {
        'en': 'LLM server',
        'de': 'LLM-Server'},
    'Fill in {placeholder}: {what}.': {
        'en': 'Fill in {placeholder}: {what}.',
        'de': '{placeholder} ausfüllen: {what}.'},
    'Ask before what the options do not allow; rulesets allow, ask or deny each request and MCP tool call': {
        'en': 'Ask before what the options do not allow; rulesets allow, ask or deny each request and MCP tool call',
        'de': 'Nachfragen bei allem, was die Optionen nicht erlauben; Regelsätze erlauben, fragen nach oder verweigern jede Anfrage und jeden MCP-Tool-Aufruf'},
    'A model on your own server (sandbox-ai llm): only its address is opened, its key stays with the proxy': {
        'en': 'A model on your own server (sandbox-ai llm): only its address is opened, its key stays with the proxy',
        'de': 'Ein Modell auf Ihrem eigenen Server (sandbox-ai llm): nur seine Adresse wird geöffnet, sein Schlüssel bleibt beim Proxy'},
    'Where the logs go (hidden from the agent), and how much of what is refused they keep': {
        'en': 'Where the logs go (hidden from the agent), and how much of what is refused they keep',
        'de': 'Wohin die Protokolle gehen (vor dem Agenten verborgen) und wie viel vom Abgelehnten sie festhalten'},
    "The project's HTTP rules need the connection proxy.": {
        'en': "The project's HTTP rules need the connection proxy.",
        'de': 'Die HTTP-Regeln des Projekts brauchen den Verbindungs-Proxy.'},
    '{how} reaches the network through the VPN {vpn}: {hosts}': {
        'en': '{how} reaches the network through the VPN {vpn}: {hosts}',
        'de': '{how} erreicht das Netz über das VPN {vpn}: {hosts}'},
    'its address and port only': {
        'en': 'its address and port only',
        'de': 'nur seine Adresse und sein Port'},
    'ask (default)': {
        'en': 'ask (default)',
        'de': 'nachfragen (Standard)'},
    'registered server': {
        'en': 'registered server',
        'de': 'registrierter Server'},
    'to fill in': {
        'en': 'to fill in',
        'de': 'auszufüllen'},
    'no network': {
        'en': 'no network',
        'de': 'kein Netz'},
    'New from preset…': {
        'en': 'New from preset…',
        'de': 'Neu aus Vorlage…'},
    'Ready boards, simple and complex (MCP servers, VPNs, LLM servers, rules), and your own': {
        'en': 'Ready boards, simple and complex (MCP servers, VPNs, LLM servers, rules), and your own',
        'de': 'Fertige Tafeln, einfache und komplexe (MCP-Server, VPNs, LLM-Server, Regeln), und Ihre eigenen'},
    '{count} project rules': {
        'en': '{count} project rules',
        'de': '{count} Projektregeln'},
    '{count} ruleset files': {
        'en': '{count} ruleset files',
        'de': '{count} Regelsatz-Dateien'},
    'VPN {vpn} · {network}': {
        'en': 'VPN {vpn} · {network}',
        'de': 'VPN {vpn} · {network}'},
    'Placeholders to fill in: {count}. Select the parts outlined in red.': {
        'en': 'Placeholders to fill in: {count}. Select the parts outlined in red.',
        'de': 'Auszufüllende Platzhalter: {count}. Wählen Sie die rot umrandeten Teile.'},
    'score {score}': {
        'en': 'score {score}',
        'de': 'Wertung {score}'},
    'New from preset': {
        'en': 'New from preset',
        'de': 'Neu aus Vorlage'},
    'Search: vpn, mcp, offline, llm, gpu…': {
        'en': 'Search: vpn, mcp, offline, llm, gpu…',
        'de': 'Suchen: vpn, mcp, offline, llm, gpu…'},
    'Save the board as a preset…': {
        'en': 'Save the board as a preset…',
        'de': 'Tafel als Vorlage speichern…'},
    'Keep the board shown now as a preset of your own (no workspaces, no secrets)': {
        'en': 'Keep the board shown now as a preset of your own (no workspaces, no secrets)',
        'de': 'Die gezeigte Tafel als eigene Vorlage behalten (ohne Arbeitsordner, ohne Geheimnisse)'},
    'Import…': {
        'en': 'Import…',
        'de': 'Importieren…'},
    'Add a preset from a JSON file (checked: options, servers, rules; never a secret)': {
        'en': 'Add a preset from a JSON file (checked: options, servers, rules; never a secret)',
        'de': 'Eine Vorlage aus einer JSON-Datei hinzufügen (geprüft: Optionen, Server, Regeln; nie ein Geheimnis)'},
    'Write the selected preset to a JSON file to pass on': {
        'en': 'Write the selected preset to a JSON file to pass on',
        'de': 'Die gewählte Vorlage in eine JSON-Datei zum Weitergeben schreiben'},
    'Export the preset': {
        'en': 'Export the preset',
        'de': 'Vorlage exportieren'},
    'Written: {path}': {
        'en': 'Written: {path}',
        'de': 'Geschrieben: {path}'},
    'Fill in: {name}': {
        'en': 'Fill in: {name}',
        'de': 'Ausfüllen: {name}'},
    'Fill in later': {
        'en': 'Fill in later',
        'de': 'Später ausfüllen'},
    'Create the board with every placeholder; fill them in on the board': {
        'en': 'Create the board with every placeholder; fill them in on the board',
        'de': 'Die Tafel mit allen Platzhaltern anlegen; sie werden auf der Tafel ausgefüllt'},
    'Save the board as a preset': {
        'en': 'Save the board as a preset',
        'de': 'Tafel als Vorlage speichern'},
    'Category': {
        'en': 'Category',
        'de': 'Kategorie'},
    '{count} to fill in': {
        'en': '{count} to fill in',
        'de': '{count} auszufüllen'},
    'Some of your presets were left out: {problems}': {
        'en': 'Some of your presets were left out: {problems}',
        'de': 'Einige Ihrer Vorlagen wurden ausgelassen: {problems}'},
    'No preset matches': {
        'en': 'No preset matches',
        'de': 'Keine Vorlage passt'},
    'Nothing to fill in.': {
        'en': 'Nothing to fill in.',
        'de': 'Nichts auszufüllen.'},
    'Import a preset': {
        'en': 'Import a preset',
        'de': 'Vorlage importieren'},
    'Delete your preset {name}?': {
        'en': 'Delete your preset {name}?',
        'de': 'Ihre Vorlage {name} löschen?'},
    'What only you know. Left empty, a value stays a placeholder on the board (shown as a problem until it is filled in); an optional part is left out.': {
        'en': 'What only you know. Left empty, a value stays a placeholder on the board (shown as a problem until it is filled in); an optional part is left out.',
        'de': 'Was nur Sie wissen. Leer gelassen bleibt ein Wert ein Platzhalter auf der Tafel (als Problem gezeigt, bis er ausgefüllt ist); ein optionaler Teil wird weggelassen.'},
    'empty: fill in later': {
        'en': 'empty: fill in later',
        'de': 'leer: später ausfüllen'},
    'The workspaces are not kept (a preset is for any project); secrets never are: a board with one is not saved.': {
        'en': 'The workspaces are not kept (a preset is for any project); secrets never are: a board with one is not saved.',
        'de': 'Die Arbeitsordner werden nicht übernommen (eine Vorlage ist für jedes Projekt); Geheimnisse nie: eine Tafel mit einem wird nicht gespeichert.'},
    'None yet: “Save the board as a preset…” keeps the board shown now.': {
        'en': 'None yet: “Save the board as a preset…” keeps the board shown now.',
        'de': 'Noch keine: „Tafel als Vorlage speichern…“ behält die gezeigte Tafel.'},
    'Not saved': {
        'en': 'Not saved',
        'de': 'Nicht gespeichert'},
    'Not imported': {
        'en': 'Not imported',
        'de': 'Nicht importiert'},
    'Presets (*.json)': {
        'en': 'Presets (*.json)',
        'de': 'Vorlagen (*.json)'},
    '(optional)': {
        'en': '(optional)',
        'de': '(optional)'},
    'To fill in:': {
        'en': 'To fill in:',
        'de': 'Auszufüllen:'},
    'My presets': {
        'en': 'My presets',
        'de': 'Meine Vorlagen'},
    'easy': {
        'en': 'easy',
        'de': 'einfach'},
    'medium': {
        'en': 'medium',
        'de': 'mittel'},
    'advanced': {
        'en': 'advanced',
        'de': 'fortgeschritten'},
    'Simple': {
        'en': 'Simple',
        'de': 'Einfach'},
    'Development': {
        'en': 'Development',
        'de': 'Entwicklung'},
    'Research': {
        'en': 'Research',
        'de': 'Recherche'},
    'Local & remote LLM': {
        'en': 'Local & remote LLM',
        'de': 'Lokale und entfernte LLMs'},
    'Privacy & VPN': {
        'en': 'Privacy & VPN',
        'de': 'Privatsphäre und VPN'},
    'Teams & policy': {
        'en': 'Teams & policy',
        'de': 'Teams und Richtlinien'},
    "No network at all: the agent works on the project's files and nothing leaves.": {
        'en': "No network at all: the agent works on the project's files and nothing leaves.",
        'de': 'Gar kein Netz: der Agent arbeitet an den Dateien des Projekts, und nichts verlässt es.'},
    "The agent's provider plus PyPI, npm and GitHub through the connection proxy; push tokens stay in the proxy.": {
        'en': "The agent's provider plus PyPI, npm and GitHub through the connection proxy; push tokens stay in the proxy.",
        'de': 'Der Anbieter des Agenten sowie PyPI, npm und GitHub über den Verbindungs-Proxy; Push-Tokens bleiben im Proxy.'},
    'Read-only review': {
        'en': 'Read-only review',
        'de': 'Review, nur lesend'},
    "The project read-only, no network but the agent's provider: for reviews and questions about code.": {
        'en': "The project read-only, no network but the agent's provider: for reviews and questions about code.",
        'de': 'Das Projekt nur lesbar, kein Netz außer dem Anbieter des Agenten: für Reviews und Fragen zum Code.'},
    'Web app with a database on this host': {
        'en': 'Web app with a database on this host',
        'de': 'Web-App mit Datenbank auf diesem Rechner'},
    "PyPI, npm and GitHub, PostgreSQL on this host's port 5432, tmux for long builds.": {
        'en': "PyPI, npm and GitHub, PostgreSQL on this host's port 5432, tmux for long builds.",
        'de': 'PyPI, npm und GitHub, PostgreSQL auf Port 5432 dieses Rechners, tmux für lange Builds.'},
    'Systems programming': {
        'en': 'Systems programming',
        'de': 'Systemprogrammierung'},
    'kernel.org, GitHub and GitLab, more memory, CPUs and processes for builds, without the default kit.': {
        'en': 'kernel.org, GitHub and GitLab, more memory, CPUs and processes for builds, without the default kit.',
        'de': 'kernel.org, GitHub und GitLab, mehr Speicher, CPUs und Prozesse für Builds, ohne das Standard-Kit.'},
    'Web research': {
        'en': 'Web research',
        'de': 'Web-Recherche'},
    'The agent searches with DuckDuckGo and reads pages through two MCP servers of the project; each page fetched is asked for.': {
        'en': 'The agent searches with DuckDuckGo and reads pages through two MCP servers of the project; each page fetched is asked for.',
        'de': 'Der Agent sucht mit DuckDuckGo und liest Seiten über zwei MCP-Server des Projekts; vor jeder abgerufenen Seite wird nachgefragt.'},
    'Browser automation': {
        'en': 'Browser automation',
        'de': 'Browser-Automatisierung'},
    'A virtual display (Xvfb) for a browser the agent drives, PyPI and npm for Playwright, and approvals for every other site.': {
        'en': 'A virtual display (Xvfb) for a browser the agent drives, PyPI and npm for Playwright, and approvals for every other site.',
        'de': 'Ein virtueller Bildschirm (Xvfb) für einen Browser, den der Agent steuert, PyPI und npm für Playwright und Freigaben für jede andere Seite.'},
    'Binary analysis, offline': {
        'en': 'Binary analysis, offline',
        'de': 'Binäranalyse, offline'},
    'Samples read-only, no network, analysis tools as MCP servers from the catalog (binwalk, YARA, capa ...).': {
        'en': 'Samples read-only, no network, analysis tools as MCP servers from the catalog (binwalk, YARA, capa ...).',
        'de': 'Proben nur lesbar, kein Netz, Analysewerkzeuge als MCP-Server aus dem Katalog (binwalk, YARA, capa ...).'},
    'Local model only': {
        'en': 'Local model only',
        'de': 'Nur ein lokales Modell'},
    'A model on this machine (Ollama, LM Studio, llama.cpp or vLLM): no cloud at all, the agent reaches nothing else.': {
        'en': 'A model on this machine (Ollama, LM Studio, llama.cpp or vLLM): no cloud at all, the agent reaches nothing else.',
        'de': 'Ein Modell auf diesem Rechner (Ollama, LM Studio, llama.cpp oder vLLM): gar keine Cloud, der Agent erreicht nichts anderes.'},
    'Remote vLLM behind Tailscale': {
        'en': 'Remote vLLM behind Tailscale',
        'de': 'Entferntes vLLM hinter Tailscale'},
    'A vLLM server in your tailnet: the sandbox runs behind the Tailscale profile and reaches only the model.': {
        'en': 'A vLLM server in your tailnet: the sandbox runs behind the Tailscale profile and reaches only the model.',
        'de': 'Ein vLLM-Server in Ihrem Tailnet: die Sandbox läuft hinter dem Tailscale-Profil und erreicht nur das Modell.'},
    'Cloud and local models (OpenCode)': {
        'en': 'Cloud and local models (OpenCode)',
        'de': 'Cloud- und lokale Modelle (OpenCode)'},
    'OpenCode with its cloud provider and a local model side by side, plus PyPI and GitHub.': {
        'en': 'OpenCode with its cloud provider and a local model side by side, plus PyPI and GitHub.',
        'de': 'OpenCode mit seinem Cloud-Anbieter und einem lokalen Modell nebeneinander, dazu PyPI und GitHub.'},
    'Everything through a WireGuard VPN': {
        'en': 'Everything through a WireGuard VPN',
        'de': 'Alles durch ein WireGuard-VPN'},
    'Every connection leaves through your WireGuard tunnel, lookups through its resolvers; a failed check stops the start.': {
        'en': 'Every connection leaves through your WireGuard tunnel, lookups through its resolvers; a failed check stops the start.',
        'de': 'Jede Verbindung geht durch Ihren WireGuard-Tunnel, Namensauflösungen über seine Resolver; eine fehlgeschlagene Prüfung stoppt den Start.'},
    'MCP servers behind different VPNs': {
        'en': 'MCP servers behind different VPNs',
        'de': 'MCP-Server hinter verschiedenen VPNs'},
    'Search through one VPN (e.g. Mullvad), page fetching through another (your own WireGuard); the agent reaches only its provider.': {
        'en': 'Search through one VPN (e.g. Mullvad), page fetching through another (your own WireGuard); the agent reaches only its provider.',
        'de': 'Suche über ein VPN (z. B. Mullvad), Seitenabruf über ein anderes (Ihr eigenes WireGuard); der Agent erreicht nur seinen Anbieter.'},
    'Tailnet-only internal services': {
        'en': 'Tailnet-only internal services',
        'de': 'Interne Dienste nur im Tailnet'},
    "Behind a Tailscale profile without exit node: the tailnet's services and names, the rest as usual.": {
        'en': "Behind a Tailscale profile without exit node: the tailnet's services and names, the rest as usual.",
        'de': 'Hinter einem Tailscale-Profil ohne Exit-Node: die Dienste und Namen des Tailnets, der Rest wie gewohnt.'},
    'Own subnet and read-only web': {
        'en': 'Own subnet and read-only web',
        'de': 'Eigenes Subnetz und nur lesendes Web'},
    'The sandbox in a network of its own, GitHub and PyPI readable, every write to the web refused by a ruleset.': {
        'en': 'The sandbox in a network of its own, GitHub and PyPI readable, every write to the web refused by a ruleset.',
        'de': 'Die Sandbox in einem eigenen Netz, GitHub und PyPI lesbar, jedes Schreiben ins Web von einem Regelsatz verweigert.'},
    'Strict rules for GitHub': {
        'en': 'Strict rules for GitHub',
        'de': 'Strenge Regeln für GitHub'},
    'GitHub with a ruleset: no gists, no issue writes, every other write asked; tokens only in the proxy.': {
        'en': 'GitHub with a ruleset: no gists, no issue writes, every other write asked; tokens only in the proxy.',
        'de': 'GitHub mit einem Regelsatz: keine Gists, keine Issue-Änderungen, bei jedem anderen Schreiben wird nachgefragt; Tokens nur im Proxy.'},
    'Long-running session': {
        'en': 'Long-running session',
        'de': 'Lang laufende Sitzung'},
    'tmux, firm limits and detailed logs for an agent that works for hours; start it as a session.': {
        'en': 'tmux, firm limits and detailed logs for an agent that works for hours; start it as a session.',
        'de': 'tmux, feste Grenzen und ausführliche Protokolle für einen Agenten, der stundenlang arbeitet; als Sitzung starten.'},
    'GPU workstation': {
        'en': 'GPU workstation',
        'de': 'GPU-Workstation'},
    'The GPU handed to the agent, generous limits, PyPI and Hugging Face for models.': {
        'en': 'The GPU handed to the agent, generous limits, PyPI and Hugging Face for models.',
        'de': 'Die GPU für den Agenten, großzügige Grenzen, PyPI und Hugging Face für Modelle.'},
    'Not a preset of this app (format {format}, version 1).': {
        'en': 'Not a preset of this app (format {format}, version 1).',
        'de': 'Keine Vorlage dieser App (Format {format}, Version 1).'},
    'Unknown keys in the preset: {keys}.': {
        'en': 'Unknown keys in the preset: {keys}.',
        'de': 'Unbekannte Schlüssel in der Vorlage: {keys}.'},
    'The preset holds what looks like a secret ({start}…): secrets stay in sandbox-ai secrets, a preset only names them.': {
        'en': 'The preset holds what looks like a secret ({start}…): secrets stay in sandbox-ai secrets, a preset only names them.',
        'de': 'Die Vorlage enthält etwas, das wie ein Geheimnis aussieht ({start}…): Geheimnisse bleiben in sandbox-ai secrets, eine Vorlage nennt sie nur.'},
    "A preset's id is lower-case letters, digits and -.": {
        'en': "A preset's id is lower-case letters, digits and -.",
        'de': 'Die Kennung einer Vorlage besteht aus Kleinbuchstaben, Ziffern und -.'},
    'A preset needs a title (at most 80 characters).': {
        'en': 'A preset needs a title (at most 80 characters).',
        'de': 'Eine Vorlage braucht einen Titel (höchstens 80 Zeichen).'},
    'The description is too long.': {
        'en': 'The description is too long.',
        'de': 'Die Beschreibung ist zu lang.'},
    'Unknown category {name}.': {
        'en': 'Unknown category {name}.',
        'de': 'Unbekannte Kategorie {name}.'},
    'Unknown difficulty {name}.': {
        'en': 'Unknown difficulty {name}.',
        'de': 'Unbekannter Schwierigkeitsgrad {name}.'},
    'Unknown agent {agent}.': {
        'en': 'Unknown agent {agent}.',
        'de': 'Unbekannter Agent {agent}.'},
    'The agent arguments hold what may be a secret.': {
        'en': 'The agent arguments hold what may be a secret.',
        'de': 'Die Agenten-Argumente enthalten etwas, das ein Geheimnis sein kann.'},
    'Options this sandbox-ai does not know: {options}.': {
        'en': 'Options this sandbox-ai does not know: {options}.',
        'de': 'Optionen, die dieses sandbox-ai nicht kennt: {options}.'},
    'A preset may not carry {options}: it copies a login in or belongs to one start.': {
        'en': 'A preset may not carry {options}: it copies a login in or belongs to one start.',
        'de': 'Eine Vorlage darf {options} nicht enthalten: es kopiert eine Anmeldung hinein oder gehört zu einem einzelnen Start.'},
    'mcp_servers must be an object of at most 16 servers.': {
        'en': 'mcp_servers must be an object of at most 16 servers.',
        'de': 'mcp_servers muss ein Objekt mit höchstens 16 Servern sein.'},
    'Positions are KIND#N: [x, y].': {
        'en': 'Positions are KIND#N: [x, y].',
        'de': 'Positionen sind KIND#N: [x, y].'},
    'The file is larger than {size} KiB.': {
        'en': 'The file is larger than {size} KiB.',
        'de': 'Die Datei ist größer als {size} KiB.'},
    '{field} must be a list of texts.': {
        'en': '{field} must be a list of texts.',
        'de': '{field} muss eine Liste von Texten sein.'},
    'Not an MCP server name: {name}.': {
        'en': 'Not an MCP server name: {name}.',
        'de': 'Kein Name eines MCP-Servers: {name}.'},
    'A placeholder has a marker, a label, a hint and whether it is optional.': {
        'en': 'A placeholder has a marker, a label, a hint and whether it is optional.',
        'de': 'Ein Platzhalter hat eine Markierung, eine Bezeichnung, einen Hinweis und die Angabe, ob er optional ist.'},
    'Offline also means no provider: use it with a local model on this host, or for tools that need no model.': {
        'en': 'Offline also means no provider: use it with a local model on this host, or for tools that need no model.',
        'de': 'Offline heißt auch: kein Anbieter. Nutzen Sie es mit einem lokalen Modell auf diesem Rechner oder für Werkzeuge, die kein Modell brauchen.'},
    "Add tokens on the Git node: the token proxy adds them to Git's requests, the agent never sees them.": {
        'en': "Add tokens on the Git node: the token proxy adds them to Git's requests, the agent never sees them.",
        'de': 'Tokens fügen Sie am Git-Knoten hinzu: der Token-Proxy setzt sie in die Anfragen von Git ein, der Agent sieht sie nie.'},
    'Nothing is asked: what the options do not allow is refused at once.': {
        'en': 'Nothing is asked: what the options do not allow is refused at once.',
        'de': 'Es wird nichts nachgefragt: was die Optionen nicht erlauben, wird sofort abgelehnt.'},
    "A host port reaches this host's loopback past the sandbox's network: open only the ports of services you started for the project.": {
        'en': "A host port reaches this host's loopback past the sandbox's network: open only the ports of services you started for the project.",
        'de': 'Ein Host-Port erreicht das Loopback dieses Rechners am Netz der Sandbox vorbei: öffnen Sie nur Ports von Diensten, die Sie für das Projekt gestartet haben.'},
    'The servers run in sandboxes of their own (uvx installs them from PyPI at their first start); the agent itself reaches only its provider.': {
        'en': 'The servers run in sandboxes of their own (uvx installs them from PyPI at their first start); the agent itself reaches only its provider.',
        'de': 'Die Server laufen in eigenen Sandboxen (uvx installiert sie beim ersten Start von PyPI); der Agent selbst erreicht nur seinen Anbieter.'},
    'The fetch server reaches every web site: it has no files of yours.': {
        'en': 'The fetch server reaches every web site: it has no files of yours.',
        'de': 'Der Fetch-Server erreicht jede Website: er hat keine Ihrer Dateien.'},
    'Playwright is a candidate: `npx playwright install chromium` inside fetches the browser; approvals ask before each new site.': {
        'en': 'Playwright is a candidate: `npx playwright install chromium` inside fetches the browser; approvals ask before each new site.',
        'de': 'Playwright bietet sich an: `npx playwright install chromium` holt drinnen den Browser; vor jeder neuen Seite fragen die Freigaben nach.'},
    'Catalog servers without network run with no network at all; the samples are mounted read-only.': {
        'en': 'Catalog servers without network run with no network at all; the samples are mounted read-only.',
        'de': 'Katalog-Server ohne Netz laufen ganz ohne Netz; die Proben werden nur lesbar eingebunden.'},
    'Claude Code needs a server with the Anthropic API (Ollama, LM Studio, llama.cpp, vLLM); a generic OpenAI-compatible server works with Codex and OpenCode.': {
        'en': 'Claude Code needs a server with the Anthropic API (Ollama, LM Studio, llama.cpp, vLLM); a generic OpenAI-compatible server works with Codex and OpenCode.',
        'de': 'Claude Code braucht einen Server mit der Anthropic-API (Ollama, LM Studio, llama.cpp, vLLM); ein allgemeiner OpenAI-kompatibler Server funktioniert mit Codex und OpenCode.'},
    'An LLM server registered with --vpn brings its VPN along: the profile here must be the same.': {
        'en': 'An LLM server registered with --vpn brings its VPN along: the profile here must be the same.',
        'de': 'Ein mit --vpn registrierter LLM-Server bringt sein VPN mit: das Profil hier muss dasselbe sein.'},
    'OpenCode lists both providers; choose the model per task.': {
        'en': 'OpenCode lists both providers; choose the model per task.',
        'de': 'OpenCode listet beide Anbieter; wählen Sie das Modell je Aufgabe.'},
    "The kill switch lets nothing out but through the tunnel; the checks compare the address seen outside with this host's.": {
        'en': "The kill switch lets nothing out but through the tunnel; the checks compare the address seen outside with this host's.",
        'de': 'Der Kill-Switch lässt nichts hinaus außer durch den Tunnel; die Prüfungen vergleichen die außen gesehene Adresse mit der dieses Rechners.'},
    "Each server's kill switch lets its connections out only through its own tunnel.": {
        'en': "Each server's kill switch lets its connections out only through its own tunnel.",
        'de': 'Der Kill-Switch jedes Servers lässt dessen Verbindungen nur durch seinen eigenen Tunnel hinaus.'},
    'Without an exit node only the tailnet (100.64.0.0/10) goes through Tailscale; its ACLs apply too.': {
        'en': 'Without an exit node only the tailnet (100.64.0.0/10) goes through Tailscale; its ACLs apply too.',
        'de': 'Ohne Exit-Node geht nur das Tailnet (100.64.0.0/10) über Tailscale; dessen ACLs gelten auch.'},
    'Choose a subnet that is free on your networks; the sandbox gets .100, the gateway .2.': {
        'en': 'Choose a subnet that is free on your networks; the sandbox gets .100, the gateway .2.',
        'de': 'Wählen Sie ein Subnetz, das in Ihren Netzen frei ist; die Sandbox bekommt .100, das Gateway .2.'},
    "The ruleset is written into the project file; a ruleset of yours or the administrator's can only narrow it further.": {
        'en': "The ruleset is written into the project file; a ruleset of yours or the administrator's can only narrow it further.",
        'de': 'Der Regelsatz wird in die Projektdatei geschrieben; ein Regelsatz von Ihnen oder vom Administrator kann ihn nur weiter einschränken.'},
    'Start it with Start as session: it keeps running when the window closes.': {
        'en': 'Start it with Start as session: it keeps running when the window closes.',
        'de': 'Starten Sie es mit „Als Sitzung starten“: es läuft weiter, wenn das Fenster geschlossen wird.'},
    'A GPU weakens the isolation; CUDA or ROCm libraries come from an image variant of yours.': {
        'en': 'A GPU weakens the isolation; CUDA or ROCm libraries come from an image variant of yours.',
        'de': 'Eine GPU schwächt die Isolation; CUDA- oder ROCm-Bibliotheken kommen aus einer eigenen Image-Variante.'},
    'Not a JSON file.': {
        'en': 'Not a JSON file.',
        'de': 'Keine JSON-Datei.'},
    'Browser MCP server': {
        'en': 'Browser MCP server',
        'de': 'Browser-MCP-Server'},
    "e.g. Playwright's (npx @playwright/mcp), defined with sandbox-ai mcp": {
        'en': "e.g. Playwright's (npx @playwright/mcp), defined with sandbox-ai mcp",
        'de': 'z. B. der von Playwright (npx @playwright/mcp), mit sandbox-ai mcp definiert'},
    'Folder with the samples (read-only)': {
        'en': 'Folder with the samples (read-only)',
        'de': 'Ordner mit den Proben (nur lesbar)'},
    'MCP server that unpacks': {
        'en': 'MCP server that unpacks',
        'de': 'MCP-Server zum Entpacken'},
    'e.g. sandbox-ai mcp add hub:binwalk': {
        'en': 'e.g. sandbox-ai mcp add hub:binwalk',
        'de': 'z. B. sandbox-ai mcp add hub:binwalk'},
    'MCP server that scans': {
        'en': 'MCP server that scans',
        'de': 'MCP-Server zum Scannen'},
    'e.g. sandbox-ai mcp add hub:yara': {
        'en': 'e.g. sandbox-ai mcp add hub:yara',
        'de': 'z. B. sandbox-ai mcp add hub:yara'},
    'Local LLM server': {
        'en': 'Local LLM server',
        'de': 'Lokaler LLM-Server'},
    'sandbox-ai llm add ollama --kind ollama --url http://127.0.0.1:11434': {
        'en': 'sandbox-ai llm add ollama --kind ollama --url http://127.0.0.1:11434',
        'de': 'sandbox-ai llm add ollama --kind ollama --url http://127.0.0.1:11434'},
    'Remote LLM server': {
        'en': 'Remote LLM server',
        'de': 'Entfernter LLM-Server'},
    'sandbox-ai llm add gpu --kind vllm --url http://100.x.y.z:8000 --vpn ts': {
        'en': 'sandbox-ai llm add gpu --kind vllm --url http://100.x.y.z:8000 --vpn ts',
        'de': 'sandbox-ai llm add gpu --kind vllm --url http://100.x.y.z:8000 --vpn ts'},
    'Tailscale profile the server is reached through': {
        'en': 'Tailscale profile the server is reached through',
        'de': 'Tailscale-Profil, über das der Server erreicht wird'},
    'sandbox-ai vpn add ts --tailscale': {
        'en': 'sandbox-ai vpn add ts --tailscale',
        'de': 'sandbox-ai vpn add ts --tailscale'},
    'WireGuard profile': {
        'en': 'WireGuard profile',
        'de': 'WireGuard-Profil'},
    'sandbox-ai vpn add home --wireguard wg.conf': {
        'en': 'sandbox-ai vpn add home --wireguard wg.conf',
        'de': 'sandbox-ai vpn add home --wireguard wg.conf'},
    'VPN profile for the search server': {
        'en': 'VPN profile for the search server',
        'de': 'VPN-Profil für den Such-Server'},
    'sandbox-ai vpn add mv --mullvad': {
        'en': 'sandbox-ai vpn add mv --mullvad',
        'de': 'sandbox-ai vpn add mv --mullvad'},
    'VPN profile for the fetch server': {
        'en': 'VPN profile for the fetch server',
        'de': 'VPN-Profil für den Fetch-Server'},
    'Tailscale profile (no exit node)': {
        'en': 'Tailscale profile (no exit node)',
        'de': 'Tailscale-Profil (ohne Exit-Node)'},
    'Folder for the logs': {
        'en': 'Folder for the logs',
        'de': 'Ordner für die Protokolle'},
    'outside the workspace': {
        'en': 'outside the workspace',
        'de': 'außerhalb des Arbeitsordners'},
    'nvidia (CDI), dri (Intel/AMD) or rocm': {
        'en': 'nvidia (CDI), dri (Intel/AMD) or rocm',
        'de': 'nvidia (CDI), dri (Intel/AMD) oder rocm'},
    'Image variant with the GPU libraries': {
        'en': 'Image variant with the GPU libraries',
        'de': 'Image-Variante mit den GPU-Bibliotheken'},
    'sandbox-ai images new NAME --from AGENT': {
        'en': 'sandbox-ai images new NAME --from AGENT',
        'de': 'sandbox-ai images new NAME --from AGENT'},
    '{path} is not valid JSON.': {
        'en': '{path} is not valid JSON.',
        'de': '{path} ist kein gültiges JSON.'},
    'Preset': {
        'en': 'Preset',
        'de': 'Vorlage'},
    'A preset fills in the next pages; everything can be changed there and on the board.': {
        'en': 'A preset fills in the next pages; everything can be changed there and on the board.',
        'de': 'Eine Vorlage füllt die nächsten Seiten aus; alles lässt sich dort und auf der Tafel ändern.'},
    'Gallery…': {
        'en': 'Gallery…',
        'de': 'Galerie…'},
    'The presets with a picture of each board, their scores and what they need': {
        'en': 'The presets with a picture of each board, their scores and what they need',
        'de': 'Die Vorlagen mit einem Bild jeder Tafel, ihren Wertungen und was sie brauchen'},
    'A preset': {
        'en': 'A preset',
        'de': 'Eine Vorlage'},
    '{name} (defined in the project)': {
        'en': '{name} (defined in the project)',
        'de': '{name} (im Projekt definiert)'},
    '{name} (to fill in on the board)': {
        'en': '{name} (to fill in on the board)',
        'de': '{name} (auf der Tafel auszufüllen)'},
    "The project's ruleset, written into .sandbox-ai.json (key rules)": {
        'en': "The project's ruleset, written into .sandbox-ai.json (key rules)",
        'de': 'Der Regelsatz des Projekts, in .sandbox-ai.json geschrieben (Schlüssel rules)'},
    'Project rules': {
        'en': 'Project rules',
        'de': 'Projektregeln'},
    'Other machines whose sandbox-ai this app manages over SSH, such as LXC containers; the web interface uses the same list.': {
        'en': 'Other machines whose sandbox-ai this app manages over SSH, such as LXC containers; the web interface uses the same list.',
        'de': 'Andere Rechner, deren sandbox-ai diese App über SSH verwaltet, etwa LXC-Container; das Web-Interface nutzt dieselbe Liste.'},
    'Add a host with its SSH address, then test it: the app runs sandbox-ai there as on this computer.': {
        'en': 'Add a host with its SSH address, then test it: the app runs sandbox-ai there as on this computer.',
        'de': 'Fügen Sie einen Host mit seiner SSH-Adresse hinzu und testen Sie ihn: Die App führt sandbox-ai dort aus wie auf diesem Rechner.'},
    "Ports on this computer's loopback (a local Git server, an LLM server), reached through a relay that only you start": {
        'en': "Ports on this computer's loopback (a local Git server, an LLM server), reached through a relay that only you start",
        'de': 'Ports auf dem Loopback dieses Rechners (ein lokaler Git-Server, ein LLM-Server), erreicht über ein Relay, das nur Sie starten'},
    'Real DNS lookups (each one logged; addresses stay closed)': {
        'en': 'Real DNS lookups (each one logged; addresses stay closed)',
        'de': 'Echte DNS-Abfragen (jede protokolliert; Adressen bleiben zu)'},
    'variable': {
        'en': 'variable',
        'de': 'Variable'},
    'secret': {
        'en': 'secret',
        'de': 'Geheimnis'},
    'NAME of the variable or secret, or the file': {
        'en': 'NAME of the variable or secret, or the file',
        'de': 'NAME der Variable oder des Geheimnisses, oder die Datei'},
    'Ports on this computer': {
        'en': 'Ports on this computer',
        'de': 'Ports auf diesem Rechner'},
    'Git login (the token, never shown)': {
        'en': 'Git login (the token, never shown)',
        'de': 'Git-Login (das Token wird nie gezeigt)'},
    'DNS': {
        'en': 'DNS',
        'de': 'DNS'},
    'A typed key or password needs the name of the secret to keep it as.': {
        'en': 'A typed key or password needs the name of the secret to keep it as.',
        'de': 'Ein eingetippter Schlüssel oder ein Passwort braucht den Namen des Geheimnisses, unter dem es aufbewahrt wird.'},
    'Checking {name}…': {
        'en': 'Checking {name}…',
        'de': 'Prüfe {name}…'},
    'Configuration': {
        'en': 'Configuration',
        'de': 'Konfiguration'},
    'Exit node': {
        'en': 'Exit node',
        'de': 'Exit-Node'},
    'Its value': {
        'en': 'Its value',
        'de': 'Sein Wert'},
    'Keys and passwords are kept as secrets of sandbox-ai and reach only the VPN container, never the agent.': {
        'en': 'Keys and passwords are kept as secrets of sandbox-ai and reach only the VPN container, never the agent.',
        'de': 'Schlüssel und Passwörter werden als Geheimnisse von sandbox-ai aufbewahrt und erreichen nur den VPN-Container, nie den Agenten.'},
    'Location': {
        'en': 'Location',
        'de': 'Standort'},
    'Login server': {
        'en': 'Login server',
        'de': 'Login-Server'},
    'Mullvad (your account number)': {
        'en': 'Mullvad (your account number)',
        'de': 'Mullvad (Ihre Kontonummer)'},
    'New VPN profile': {
        'en': 'New VPN profile',
        'de': 'Neues VPN-Profil'},
    'New VPN profile…': {
        'en': 'New VPN profile…',
        'de': 'Neues VPN-Profil…'},
    'OpenVPN (an .ovpn file)': {
        'en': 'OpenVPN (an .ovpn file)',
        'de': 'OpenVPN (eine .ovpn-Datei)'},
    'Provider': {
        'en': 'Provider',
        'de': 'Anbieter'},
    'Remove the VPN profile {name}?': {
        'en': 'Remove the VPN profile {name}?',
        'de': 'Das VPN-Profil {name} entfernen?'},
    'Secret for USER:PASSWORD (optional)': {
        'en': 'Secret for USER:PASSWORD (optional)',
        'de': 'Geheimnis für BENUTZER:PASSWORT (optional)'},
    'Secret for the account number': {
        'en': 'Secret for the account number',
        'de': 'Geheimnis für die Kontonummer'},
    'Secret for the auth key (optional: else log in in a terminal)': {
        'en': 'Secret for the auth key (optional: else log in in a terminal)',
        'de': 'Geheimnis für den Auth-Key (optional: sonst Anmeldung in einem Terminal)'},
    'Tailscale or Headscale (an auth key)': {
        'en': 'Tailscale or Headscale (an auth key)',
        'de': 'Tailscale oder Headscale (ein Auth-Key)'},
    'VPN configuration': {
        'en': 'VPN configuration',
        'de': 'VPN-Konfiguration'},
    'VPN profiles (sandbox-ai vpn)': {
        'en': 'VPN profiles (sandbox-ai vpn)',
        'de': 'VPN-Profile (sandbox-ai vpn)'},
    'WireGuard (a wg-quick file: Proton, IVPN, AirVPN, your own server)': {
        'en': 'WireGuard (a wg-quick file: Proton, IVPN, AirVPN, your own server)',
        'de': 'WireGuard (eine wg-quick-Datei: Proton, IVPN, AirVPN, Ihr eigener Server)'},
    'typed here: kept as that secret (or leave it empty if it is kept already)': {
        'en': 'typed here: kept as that secret (or leave it empty if it is kept already)',
        'de': 'hier eingetippt: als dieses Geheimnis aufbewahrt (oder leer lassen, wenn es schon aufbewahrt ist)'},
    "sandbox-ai's presets (sandbox-ai presets)": {
        'en': "sandbox-ai's presets (sandbox-ai presets)",
        'de': "Die Presets von sandbox-ai (sandbox-ai presets)"},
    '{path} is not trusted: look at the board, then Apply (it writes and trusts what you see) before you start it.': {
        'en': '{path} is not trusted: look at the board, then Apply (it writes and trusts what you see) before you start it.',
        'de': '{path} ist nicht vertraut: Sehen Sie sich das Board an und übernehmen Sie es mit Anwenden (das schreibt und vertraut, was Sie sehen), bevor Sie es starten.'},
    '{name}: the copy was replaced while it was written; nothing was placed.': {
        'en': '{name}: the copy was replaced while it was written; nothing was placed.',
        'de': '{name}: Die Kopie wurde ersetzt, während sie geschrieben wurde; nichts wurde abgelegt.'},
    'Set up step by step…': {
        'en': 'Set up step by step…',
        'de': 'Schritt für Schritt einrichten…'},
    'The guide asks what matters for this part, one question at a time': {
        'en': 'The guide asks what matters for this part, one question at a time',
        'de': 'Die Führung fragt, was für diesen Teil wichtig ist, eine Frage nach der anderen'},
    'Start an agent in a sandbox: pick the project folder and the agent, see what the start will do, and start it in the app, in a terminal window or as a session that keeps running.': {
        'en': 'Start an agent in a sandbox: pick the project folder and the agent, see what the start will do, and start it in the app, in a terminal window or as a session that keeps running.',
        'de': 'Einen Agenten in einer Sandbox starten: Projektordner und Agent wählen, sehen, was der Start tun wird, und ihn in der App, in einem Terminalfenster oder als weiterlaufende Sitzung starten.'},
    "Choose a project folder, then press Dry run to see the launcher's verdict without starting.": {
        'en': "Choose a project folder, then press Dry run to see the launcher's verdict without starting.",
        'de': 'Wählen Sie einen Projektordner und drücken Sie dann Probelauf, um das Urteil des Launchers ohne Start zu sehen.'},
    'One sandbox as nodes: the agent, its folders, the proxy, the network and everything else. The checks below follow every change; Apply writes .sandbox-ai.json after showing it.': {
        'en': 'One sandbox as nodes: the agent, its folders, the proxy, the network and everything else. The checks below follow every change; Apply writes .sandbox-ai.json after showing it.',
        'de': 'Eine Sandbox als Knoten: der Agent, seine Ordner, der Proxy, das Netz und alles andere. Die Prüfungen darunter folgen jeder Änderung; Übernehmen schreibt .sandbox-ai.json, nachdem es sie gezeigt hat.'},
    'Double-click a part in the list on the left to add it: the guide helps you set it up.': {
        'en': 'Double-click a part in the list on the left to add it: the guide helps you set it up.',
        'de': 'Doppelklicken Sie links auf einen Teil, um ihn hinzuzufügen: Die Führung hilft beim Einrichten.'},
    'Everything that runs or is kept: one-off runs, sessions, MCP servers, with pause, resume, stop and attach.': {
        'en': 'Everything that runs or is kept: one-off runs, sessions, MCP servers, with pause, resume, stop and attach.',
        'de': 'Alles, was läuft oder aufbewahrt wird: einmalige Läufe, Sitzungen, MCP-Server, mit Anhalten, Fortsetzen, Beenden und Anhängen.'},
    'Select a session to see its options and history.': {
        'en': 'Select a session to see its options and history.',
        'de': 'Wählen Sie eine Sitzung, um ihre Optionen und ihren Verlauf zu sehen.'},
    'Questions of running sandboxes: a connection that is not allowed waits here for your answer, once, for the run, or always.': {
        'en': 'Questions of running sandboxes: a connection that is not allowed waits here for your answer, once, for the run, or always.',
        'de': 'Fragen laufender Sandboxen: Eine nicht erlaubte Verbindung wartet hier auf Ihre Antwort, einmal, für den Lauf oder immer.'},
    'Leave the page open while an agent works, or let the tray tell you.': {
        'en': 'Leave the page open while an agent works, or let the tray tell you.',
        'de': 'Lassen Sie die Seite offen, während ein Agent arbeitet, oder lassen Sie sich vom Infobereich benachrichtigen.'},
    'The terminals of builds, logins and sandboxes started in the app, and every running start to attach to, however it was started.': {
        'en': 'The terminals of builds, logins and sandboxes started in the app, and every running start to attach to, however it was started.',
        'de': 'Die Terminals der Builds, Anmeldungen und in der App gestarteten Sandboxen, und jeder laufende Start zum Verbinden, gleich wie er gestartet wurde.'},
    'Ctrl-b d detaches from a session; it keeps running.': {
        'en': 'Ctrl-b d detaches from a session; it keeps running.',
        'de': 'Strg-b d löst sich von einer Sitzung; sie läuft weiter.'},
    'Every connection the proxy saw: allowed, asked, refused, with the request and the rule that decided.': {
        'en': 'Every connection the proxy saw: allowed, asked, refused, with the request and the rule that decided.',
        'de': 'Jede Verbindung, die der Proxy sah: erlaubt, erfragt, abgelehnt, mit der Anfrage und der Regel, die entschied.'},
    'Filter by host to see what an agent reached.': {
        'en': 'Filter by host to see what an agent reached.',
        'de': 'Filtern Sie nach Host, um zu sehen, was ein Agent erreicht hat.'},
    'What happened in a run, in order: starts, tool calls, questions, connections, the taint.': {
        'en': 'What happened in a run, in order: starts, tool calls, questions, connections, the taint.',
        'de': 'Was in einem Lauf geschah, der Reihe nach: Starts, Werkzeugaufrufe, Fragen, Verbindungen, der Taint.'},
    'Pick a run to follow it from start to end.': {
        'en': 'Pick a run to follow it from start to end.',
        'de': 'Wählen Sie einen Lauf, um ihm vom Anfang bis zum Ende zu folgen.'},
    'Tokens, requests and tool calls per run and per agent.': {
        'en': 'Tokens, requests and tool calls per run and per agent.',
        'de': 'Tokens, Anfragen und Werkzeugaufrufe je Lauf und je Agent.'},
    'Compare two runs of the same project.': {
        'en': 'Compare two runs of the same project.',
        'de': 'Vergleichen Sie zwei Läufe desselben Projekts.'},
    "The files of a sandbox's HOME and workspaces, to look into and to copy in or out.": {
        'en': "The files of a sandbox's HOME and workspaces, to look into and to copy in or out.",
        'de': 'Die Dateien des HOME und der Arbeitsordner einer Sandbox, zum Hineinsehen und zum Hinein- oder Herauskopieren.'},
    'Copy a file into a running sandbox without giving it a mount.': {
        'en': 'Copy a file into a running sandbox without giving it a mount.',
        'de': 'Kopieren Sie eine Datei in eine laufende Sandbox, ohne ihr einen Mount zu geben.'},
    'Skills, agents, commands, prompts, memory templates, hooks and plugins, in kits a project uses with --kit.': {
        'en': 'Skills, agents, commands, prompts, memory templates, hooks and plugins, in kits a project uses with --kit.',
        'de': 'Skills, Agenten, Befehle, Prompts, Gedächtnis-Vorlagen, Hooks und Plugins, in Kits, die ein Projekt mit --kit nutzt.'},
    'Create a kit of your own, add a skill, and use it in the current project.': {
        'en': 'Create a kit of your own, add a skill, and use it in the current project.',
        'de': 'Legen Sie ein eigenes Kit an, fügen Sie einen Skill hinzu und nutzen Sie es im aktuellen Projekt.'},
    'MCP servers in sandboxes of their own, from your definitions and the catalogs, with their network and folders.': {
        'en': 'MCP servers in sandboxes of their own, from your definitions and the catalogs, with their network and folders.',
        'de': 'MCP-Server in eigenen Sandboxen, aus Ihren Definitionen und den Katalogen, mit ihrem Netz und ihren Ordnern.'},
    'Add a search server from the catalog and connect it on the board.': {
        'en': 'Add a search server from the catalog and connect it on the board.',
        'de': 'Fügen Sie einen Suchserver aus dem Katalog hinzu und verbinden Sie ihn auf der Tafel.'},
    "The agents' images: build, update, and extend them with packages of your own.": {
        'en': "The agents' images: build, update, and extend them with packages of your own.",
        'de': 'Die Images der Agenten: bauen, aktualisieren und um eigene Pakete erweitern.'},
    'Build the image of the agent you use, once.': {
        'en': 'Build the image of the agent you use, once.',
        'de': 'Bauen Sie einmal das Image des Agenten, den Sie nutzen.'},
    'Logins, secrets and Git tokens: kept on this computer, handed to a sandbox only when a start names them.': {
        'en': 'Logins, secrets and Git tokens: kept on this computer, handed to a sandbox only when a start names them.',
        'de': 'Anmeldungen, Geheimnisse und Git-Tokens: auf diesem Rechner aufbewahrt, einer Sandbox nur übergeben, wenn ein Start sie nennt.'},
    'Log in to your agent once; every sandbox gets the login from here.': {
        'en': 'Log in to your agent once; every sandbox gets the login from here.',
        'de': 'Melden Sie sich einmal bei Ihrem Agenten an; jede Sandbox bekommt die Anmeldung von hier.'},
    'What sandbox-ai needs on this computer, and whether it is there: Podman, Bubblewrap, the image, the network.': {
        'en': 'What sandbox-ai needs on this computer, and whether it is there: Podman, Bubblewrap, the image, the network.',
        'de': 'Was sandbox-ai auf diesem Rechner braucht und ob es da ist: Podman, Bubblewrap, das Image, das Netz.'},
    'Run the checks; each problem comes with what fixes it.': {
        'en': 'Run the checks; each problem comes with what fixes it.',
        'de': 'Starten Sie die Prüfungen; zu jedem Problem steht, was es behebt.'},
    'The host folder the agent works in. The first workspace is /work in the sandbox, further ones /workspaces/NAME.': {
        'en': 'The host folder the agent works in. The first workspace is /work in the sandbox, further ones /workspaces/NAME.',
        'de': 'Der Ordner des Hosts, in dem der Agent arbeitet. Der erste Arbeitsordner ist /work in der Sandbox, weitere /workspaces/NAME.'},
    'Read-only': {
        'en': 'Read-only',
        'de': 'Schreibgeschützt'},
    'The agent can read the workspaces but not change them (this holds for every workspace).': {
        'en': 'The agent can read the workspaces but not change them (this holds for every workspace).',
        'de': 'Der Agent kann die Arbeitsordner lesen, aber nicht ändern (das gilt für jeden Arbeitsordner).'},
    'What the sandbox gets in addition to its workspaces. Never a folder with keys or logins.': {
        'en': 'What the sandbox gets in addition to its workspaces. Never a folder with keys or logins.',
        'de': 'Was die Sandbox zusätzlich zu ihren Arbeitsordnern bekommt. Nie ein Ordner mit Schlüsseln oder Anmeldungen.'},
    'Where it appears: below /mnt or /opt, or in /work.': {
        'en': 'Where it appears: below /mnt or /opt, or in /work.',
        'de': 'Wo es erscheint: unter /mnt oder /opt, oder in /work.'},
    'Read-only unless the agent has to write there.': {
        'en': 'Read-only unless the agent has to write there.',
        'de': 'Schreibgeschützt, außer der Agent muss dort schreiben.'},
    'HOME of your own': {
        'en': 'HOME of your own',
        'de': 'Eigenes HOME'},
    "A folder that is the sandbox's HOME and stays. Empty: the start keeps a HOME of its own anyway.": {
        'en': "A folder that is the sandbox's HOME and stays. Empty: the start keeps a HOME of its own anyway.",
        'de': 'Ein Ordner, der das HOME der Sandbox ist und bleibt. Leer: Der Start behält ohnehin ein eigenes HOME.'},
    "The agent's login: found automatically (the login store's or this computer's), always copied, or none.": {
        'en': "The agent's login: found automatically (the login store's or this computer's), always copied, or none.",
        'de': 'Die Anmeldung des Agenten: automatisch gefunden (die des Login-Stores oder dieses Rechners), immer kopiert, oder keine.'},
    'Kits by name, one per line: their skills, agents, commands and hooks join the sandbox (see the Kits & plugins page).': {
        'en': 'Kits by name, one per line: their skills, agents, commands and hooks join the sandbox (see the Kits & plugins page).',
        'de': 'Kits nach Namen, eines je Zeile: Ihre Skills, Agenten, Befehle und Hooks kommen in die Sandbox (siehe die Seite Kits & Plugins).'},
    'Without the default kit': {
        'en': 'Without the default kit',
        'de': 'Ohne das Standard-Kit'},
    'Leave out the kit every sandbox gets.': {
        'en': 'Leave out the kit every sandbox gets.',
        'de': 'Das Kit weglassen, das jede Sandbox bekommt.'},
    'Without project memory': {
        'en': 'Without project memory',
        'de': 'Ohne Projektgedächtnis'},
    'The agent starts without the notes it kept for this project.': {
        'en': 'The agent starts without the notes it kept for this project.',
        'de': 'Der Agent startet ohne die Notizen, die er für dieses Projekt behalten hat.'},
    'The coding agent that runs in the sandbox.': {
        'en': 'The coding agent that runs in the sandbox.',
        'de': 'Der Coding-Agent, der in der Sandbox läuft.'},
    'Act without asking': {
        'en': 'Act without asking',
        'de': 'Ohne Rückfrage handeln'},
    'The agent no longer asks before it runs a tool. The sandbox still holds, but it acts on its own.': {
        'en': 'The agent no longer asks before it runs a tool. The sandbox still holds, but it acts on its own.',
        'de': 'Der Agent fragt nicht mehr, bevor er ein Werkzeug ausführt. Die Sandbox hält weiter, aber er handelt selbständig.'},
    'Questions': {
        'en': 'Questions',
        'de': 'Rückfragen'},
    'Ask you before a connection the options do not allow, or refuse it at once.': {
        'en': 'Ask you before a connection the options do not allow, or refuse it at once.',
        'de': 'Sie vor einer Verbindung fragen, die die Optionen nicht erlauben, oder sie sofort ablehnen.'},
    'Rulesets': {
        'en': 'Rulesets',
        'de': 'Regelwerke'},
    "Files of rules, one per line: they allow, ask or deny requests, MCP tool calls and the agent's own tools.": {
        'en': "Files of rules, one per line: they allow, ask or deny requests, MCP tool calls and the agent's own tools.",
        'de': 'Regeldateien, eine je Zeile: Sie erlauben, erfragen oder verbieten Anfragen, MCP-Werkzeugaufrufe und die eigenen Werkzeuge des Agenten.'},
    'Every connection passes it: logged, asked about, pushes checked. Turn it off only if a tool cannot work with it.': {
        'en': 'Every connection passes it: logged, asked about, pushes checked. Turn it off only if a tool cannot work with it.',
        'de': 'Jede Verbindung geht durch ihn: protokolliert, erfragt, Pushes geprüft. Schalten Sie ihn nur ab, wenn ein Werkzeug nicht mit ihm arbeiten kann.'},
    'Hosts without TLS inspection': {
        'en': 'Hosts without TLS inspection',
        'de': 'Hosts ohne TLS-Prüfung'},
    'Hosts whose TLS the proxy passes through unopened, one per line (for tools that pin their certificates).': {
        'en': 'Hosts whose TLS the proxy passes through unopened, one per line (for tools that pin their certificates).',
        'de': 'Hosts, deren TLS der Proxy ungeöffnet durchlässt, einer je Zeile (für Werkzeuge, die ihre Zertifikate festlegen).'},
    'Which pushes are checked for private data before they leave.': {
        'en': 'Which pushes are checked for private data before they leave.',
        'de': 'Welche Pushes vor dem Hinausgehen auf private Daten geprüft werden.'},
    'Your name and e-mail': {
        'en': 'Your name and e-mail',
        'de': 'Ihr Name und Ihre E-Mail'},
    'Commits in the sandbox carry your Git identity.': {
        'en': 'Commits in the sandbox carry your Git identity.',
        'de': 'Commits in der Sandbox tragen Ihre Git-Identität.'},
    'Push tokens': {
        'en': 'Push tokens',
        'de': 'Push-Tokens'},
    'URL=VARIABLE, one per line: a token from your environment for that Git server.': {
        'en': 'URL=VARIABLE, one per line: a token from your environment for that Git server.',
        'de': 'URL=VARIABLE, eines je Zeile: ein Token aus Ihrer Umgebung für diesen Git-Server.'},
    'Token proxy': {
        'en': 'Token proxy',
        'de': 'Token-Proxy'},
    'The proxy adds the token to each push: the agent never sees it.': {
        'en': 'The proxy adds the token to each push: the agent never sees it.',
        'de': 'Der Proxy fügt das Token jedem Push hinzu: Der Agent sieht es nie.'},
    'The MCP server by name (the MCP servers page lists them); it runs in a sandbox of its own.': {
        'en': 'The MCP server by name (the MCP servers page lists them); it runs in a sandbox of its own.',
        'de': 'Der MCP-Server nach Namen (die Seite MCP-Server listet sie); er läuft in einer eigenen Sandbox.'},
    'A server of sandbox-ai llm, NAME or NAME:MODEL: only its address is opened.': {
        'en': 'A server of sandbox-ai llm, NAME or NAME:MODEL: only its address is opened.',
        'de': 'Ein Server von sandbox-ai llm, NAME oder NAME:MODELL: Nur seine Adresse wird geöffnet.'},
    'Further hosts the agent may reach over HTTPS, one per line.': {
        'en': 'Further hosts the agent may reach over HTTPS, one per line.',
        'de': 'Weitere Hosts, die der Agent über HTTPS erreichen darf, einer je Zeile.'},
    'PyPI': {
        'en': 'PyPI',
        'de': 'PyPI'},
    'Python packages.': {
        'en': 'Python packages.',
        'de': 'Python-Pakete.'},
    'GitHub': {
        'en': 'GitHub',
        'de': 'GitHub'},
    'GitHub and its downloads.': {
        'en': 'GitHub and its downloads.',
        'de': 'GitHub und seine Downloads.'},
    'Offline': {
        'en': 'Offline',
        'de': 'Offline'},
    "No network at all, not even the agent's provider.": {
        'en': "No network at all, not even the agent's provider.",
        'de': 'Gar kein Netz, nicht einmal der Anbieter des Agenten.'},
    'VPN profile': {
        'en': 'VPN profile',
        'de': 'VPN-Profil'},
    'A profile of sandbox-ai vpn: everything leaves through it.': {
        'en': 'A profile of sandbox-ai vpn: everything leaves through it.',
        'de': 'Ein Profil von sandbox-ai vpn: Alles geht durch es hinaus.'},
    'When the check fails': {
        'en': 'When the check fails',
        'de': 'Wenn die Prüfung scheitert'},
    'Stop the start, or only warn.': {
        'en': 'Stop the start, or only warn.',
        'de': 'Den Start abbrechen oder nur warnen.'},
    'Devices': {
        'en': 'Devices',
        'de': 'Geräte'},
    'Host devices, one per line (/dev/ttyUSB0, /dev/kvm): each weakens the isolation.': {
        'en': 'Host devices, one per line (/dev/ttyUSB0, /dev/kvm): each weakens the isolation.',
        'de': 'Geräte des Hosts, eines je Zeile (/dev/ttyUSB0, /dev/kvm): Jedes schwächt die Isolation.'},
    'A GPU for the agent.': {
        'en': 'A GPU for the agent.',
        'de': 'Eine GPU für den Agenten.'},
    'At most this much, e.g. 8g.': {
        'en': 'At most this much, e.g. 8g.',
        'de': 'Höchstens so viel, z. B. 8g.'},
    'At most this many CPUs, e.g. 4.': {
        'en': 'At most this many CPUs, e.g. 4.',
        'de': 'Höchstens so viele CPUs, z. B. 4.'},
    'At most this many processes.': {
        'en': 'At most this many processes.',
        'de': 'Höchstens so viele Prozesse.'},
    'Virtual display': {
        'en': 'Virtual display',
        'de': 'Virtueller Bildschirm'},
    'A display for GUI programs and browsers in the sandbox.': {
        'en': 'A display for GUI programs and browsers in the sandbox.',
        'de': 'Ein Bildschirm für GUI-Programme und Browser in der Sandbox.'},
    'Watch it': {
        'en': 'Watch it',
        'de': 'Zuschauen'},
    "A folder for the display's VNC socket, to watch what happens.": {
        'en': "A folder for the display's VNC socket, to watch what happens.",
        'de': 'Ein Ordner für den VNC-Socket des Bildschirms, um zuzusehen, was geschieht.'},
    'tmux': {
        'en': 'tmux',
        'de': 'tmux'},
    'Run the agent in tmux inside the sandbox (detach and attach again).': {
        'en': 'Run the agent in tmux inside the sandbox (detach and attach again).',
        'de': 'Den Agenten in tmux in der Sandbox laufen lassen (lösen und wieder anhängen).'},
    'Where the connection logs go; the agent cannot read them.': {
        'en': 'Where the connection logs go; the agent cannot read them.',
        'de': 'Wohin die Verbindungsprotokolle gehen; der Agent kann sie nicht lesen.'},
    'More detail': {
        'en': 'More detail',
        'de': 'Mehr Einzelheiten'},
    'Keep what was refused in more detail.': {
        'en': 'Keep what was refused in more detail.',
        'de': 'Was abgelehnt wurde, ausführlicher festhalten.'},
    "Podman (containers) or Bubblewrap (the host's programs).": {
        'en': "Podman (containers) or Bubblewrap (the host's programs).",
        'de': 'Podman (Container) oder Bubblewrap (die Programme des Hosts).'},
    'Image variant': {
        'en': 'Image variant',
        'de': 'Image-Variante'},
    'An image of your own with more packages (Images page).': {
        'en': 'An image of your own with more packages (Images page).',
        'de': 'Ein eigenes Image mit mehr Paketen (Seite Images).'},
    '{what} is not set yet': {
        'en': '{what} is not set yet',
        'de': '{what} ist noch nicht gesetzt'},
    'automatic': {
        'en': 'automatic',
        'de': 'automatisch'},
    'always copy it': {
        'en': 'always copy it',
        'de': 'immer kopieren'},
    'ask me': {
        'en': 'ask me',
        'de': 'mich fragen'},
    'refuse': {
        'en': 'refuse',
        'de': 'ablehnen'},
    'to public hosts': {
        'en': 'to public hosts',
        'de': 'an öffentliche Hosts'},
    'every push': {
        'en': 'every push',
        'de': 'jeder Push'},
    'stop': {
        'en': 'stop',
        'de': 'abbrechen'},
    'warn': {
        'en': 'warn',
        'de': 'warnen'},
    'as the start decides': {
        'en': 'as the start decides',
        'de': 'wie der Start entscheidet'},
    'always': {
        'en': 'always',
        'de': 'immer'},
    'never': {
        'en': 'never',
        'de': 'nie'},
    'Set up {name}': {
        'en': 'Set up {name}',
        'de': '{name} einrichten'},
    'Back': {
        'en': 'Back',
        'de': 'Zurück'},
    'Next': {
        'en': 'Next',
        'de': 'Weiter'},
    'Close the tour': {
        'en': 'Close the tour',
        'de': 'Tour beenden'},
    'Guided tour · {number} of {count}': {
        'en': 'Guided tour · {number} of {count}',
        'de': 'Geführte Tour · {number} von {count}'},
    'Try: {what}': {
        'en': 'Try: {what}',
        'de': 'Probieren Sie: {what}'},
    'Set up: {name}': {
        'en': 'Set up: {name}',
        'de': 'Einrichten: {name}'},
    'Step {number} of {count}': {
        'en': 'Step {number} of {count}',
        'de': 'Schritt {number} von {count}'},
    'Finish': {
        'en': 'Finish',
        'de': 'Fertig'},
    '{count} question(s); each can be changed later in the inspector.': {
        'en': '{count} question(s); each can be changed later in the inspector.',
        'de': '{count} Frage(n); jede lässt sich später im Inspektor ändern.'},
    'What the board still finds': {
        'en': 'What the board still finds',
        'de': 'Was die Tafel noch findet'},
    'Done': {
        'en': 'Done',
        'de': 'Fertig'},
    'Nothing: the node is ready.': {
        'en': 'Nothing: the node is ready.',
        'de': 'Nichts: Der Knoten ist bereit.'},
    'Help set up each new part of the board, one question at a time': {
        'en': 'Help set up each new part of the board, one question at a time',
        'de': 'Beim Einrichten jedes neuen Teils der Tafel helfen, eine Frage nach der anderen'},
    'Offer the guided tour at the first start': {
        'en': 'Offer the guided tour at the first start',
        'de': 'Beim ersten Start die geführte Tour anbieten'},
    'Guided tour': {
        'en': 'Guided tour',
        'de': 'Geführte Tour'},
    'Guide': {
        'en': 'Guide',
        'de': 'Führung'},
    'Your AI agent works inside a sandbox.': {
        'en': 'Your AI agent works inside a sandbox.',
        'de': 'Ihr KI-Agent arbeitet in einer Sandbox.'},
    'Every connection goes through the proxy and its allow-list.': {
        'en': 'Every connection goes through the proxy and its allow-list.',
        'de': 'Jede Verbindung geht durch den Proxy und seine Allow-List.'},
    'Anything else is blocked, or waits for your approval.': {
        'en': 'Anything else is blocked, or waits for your approval.',
        'de': 'Alles andere wird gesperrt oder wartet auf Ihre Freigabe.'},
    'Let AI agents work freely. Your machine stays protected.': {
        'en': 'Let AI agents work freely. Your machine stays protected.',
        'de': 'KI-Agenten arbeiten frei. Ihr Rechner bleibt geschützt.'},
    'Allow-list': {
        'en': 'Allow-list',
        'de': 'Allow-List'},
    'Allow {host}?': {
        'en': 'Allow {host}?',
        'de': '{host} erlauben?'},
    'Skip': {
        'en': 'Skip',
        'de': 'Überspringen'},
    'Watch the intro again': {
        'en': 'Watch the intro again',
        'de': 'Intro noch einmal ansehen'},
    'Press Esc to skip': {
        'en': 'Press Esc to skip',
        'de': 'Esc überspringt'},
    'Play the intro animation at start (Esc skips it)': {
        'en': 'Play the intro animation at start (Esc skips it)',
        'de': 'Beim Start die Intro-Animation zeigen (Esc überspringt sie)'},
    'plugin {name}': {
        'en': 'plugin {name}',
        'de': 'Plugin {name}'},
    'every tool': {
        'en': 'every tool',
        'de': 'jedes Werkzeug'},
    'Add hook': {
        'en': 'Add hook',
        'de': 'Hook hinzufügen'},
    'A script runs at the event for the tools the matcher names; a hooks.json brings its own definitions; an OpenCode plugin is a .js, .mjs or .ts module. The hook runs only once you have reviewed and approved it.': {
        'en': 'A script runs at the event for the tools the matcher names; a hooks.json brings its own definitions; an OpenCode plugin is a .js, .mjs or .ts module. The hook runs only once you have reviewed and approved it.',
        'de': 'Ein Skript läuft beim Ereignis für die Werkzeuge, die der Matcher nennt; eine hooks.json bringt eigene Definitionen mit; ein OpenCode-Plugin ist ein .js-, .mjs- oder .ts-Modul. Der Hook läuft erst, wenn Sie ihn geprüft und freigegeben haben.'},
    'check.sh, hooks.json or plugin.js': {
        'en': 'check.sh, hooks.json or plugin.js',
        'de': 'check.sh, hooks.json oder plugin.js'},
    'File…': {
        'en': 'File…',
        'de': 'Datei…'},
    'every tool; e.g. Bash or Write|Edit': {
        'en': 'every tool; e.g. Bash or Write|Edit',
        'de': 'jedes Werkzeug; z. B. Bash oder Write|Edit'},
    'Event (a script)': {
        'en': 'Event (a script)',
        'de': 'Ereignis (ein Skript)'},
    'Matcher (a script)': {
        'en': 'Matcher (a script)',
        'de': 'Matcher (ein Skript)'},
    'Hook file': {
        'en': 'Hook file',
        'de': 'Hook-Datei'},
    'Add hook…': {
        'en': 'Add hook…',
        'de': 'Hook hinzufügen…'},
    'sandbox-ai kits add-hook: a hook script, a hooks.json or an OpenCode plugin; it runs once approved': {
        'en': 'sandbox-ai kits add-hook: a hook script, a hooks.json or an OpenCode plugin; it runs once approved',
        'de': 'sandbox-ai kits add-hook: ein Hook-Skript, eine hooks.json oder ein OpenCode-Plugin; es läuft nach der Freigabe'},
    'Review and approve hooks…': {
        'en': 'Review and approve hooks…',
        'de': 'Hooks prüfen und freigeben…'},
    'Review the hooks of {name} in the terminal, then Refresh.': {
        'en': 'Review the hooks of {name} in the terminal, then Refresh.',
        'de': 'Prüfen Sie die Hooks von {name} im Terminal, dann Aktualisieren.'},
    'This sandbox-ai has no kit hooks yet; update it.': {
        'en': 'This sandbox-ai has no kit hooks yet; update it.',
        'de': 'Dieses sandbox-ai kennt noch keine Kit-Hooks; aktualisieren Sie es.'},
    'The kit has no hooks.': {
        'en': 'The kit has no hooks.',
        'de': 'Das Kit hat keine Hooks.'},
    'sandbox-ai kits trust-hooks, in a terminal: shows every hook and the full text of every file; you approve there': {
        'en': 'sandbox-ai kits trust-hooks, in a terminal: shows every hook and the full text of every file; you approve there',
        'de': 'sandbox-ai kits trust-hooks, in einem Terminal: zeigt jeden Hook und den vollen Text jeder Datei; dort geben Sie frei'},
    'The kit has no skills, plugins or hooks.': {
        'en': 'The kit has no skills, plugins or hooks.',
        'de': 'Das Kit hat keine Skills, Plugins oder Hooks.'},
    'hook {name} ({agent})': {
        'en': 'hook {name} ({agent})',
        'de': 'Hook {name} ({agent})'},
    'cannot run': {
        'en': 'cannot run',
        'de': 'kann nicht laufen'},
    "sandbox-ai's own skill has no hooks to approve.": {
        'en': "sandbox-ai's own skill has no hooks to approve.",
        'de': 'Der eigene Skill von sandbox-ai hat keine Hooks zum Freigeben.'},
    'hooks: {count} (not approved)': {
        'en': 'hooks: {count} (not approved)',
        'de': 'Hooks: {count} (nicht freigegeben)'},
    'hooks: {count}': {
        'en': 'hooks: {count}',
        'de': 'Hooks: {count}'},
    'approved': {
        'en': 'approved',
        'de': 'freigegeben'},
    'not approved: they do not run': {
        'en': 'not approved: they do not run',
        'de': 'nicht freigegeben: sie laufen nicht'},
    'Hooks: {count} ({state})': {
        'en': 'Hooks: {count} ({state})',
        'de': 'Hooks: {count} ({state})'},
    'Learn this area': {
        'en': 'Learn this area',
        'de': 'Diesen Bereich kennenlernen'},
    'A tour of this page: each control and how to use it': {
        'en': 'A tour of this page: each control and how to use it',
        'de': 'Eine Tour dieser Seite: jedes Bedienelement und wie man es nutzt'},
    'Tour of {area} · {number} of {count}': {
        'en': 'Tour of {area} · {number} of {count}',
        'de': 'Tour: {area} · {number} von {count}'},
    'Tour of this page': {
        'en': 'Tour of this page',
        'de': 'Tour dieser Seite'},
    'Area tours': {
        'en': 'Area tours',
        'de': 'Bereichs-Touren'},
    'Tour of this area': {
        'en': 'Tour of this area',
        'de': 'Tour dieses Bereichs'},
    'Only icons': {
        'en': 'Only icons',
        'de': 'Nur Symbole'},
    'Fold all sections': {
        'en': 'Fold all sections',
        'de': 'Alle Abschnitte einklappen'},
    'Unfold all sections': {
        'en': 'Unfold all sections',
        'de': 'Alle Abschnitte ausklappen'},
    'Show the page titles': {
        'en': 'Show the page titles',
        'de': 'Seitentitel zeigen'},
    'Only icons: more room for the pages': {
        'en': 'Only icons: more room for the pages',
        'de': 'Nur Symbole: mehr Platz für die Seiten'},
    'Navigation': {
        'en': 'Navigation',
        'de': 'Navigation'},
    'Effort': {
        'en': 'Effort',
        'de': 'Gründlichkeit'},
    '{agent} sets its effort per model in its own configuration': {
        'en': '{agent} sets its effort per model in its own configuration',
        'de': '{agent} stellt die Gründlichkeit pro Modell in seiner eigenen Konfiguration ein'},
    "The agent's model: pick one it knows or type another (Codex: its model name, OpenCode: provider/model); empty: its default.": {
        'en': "The agent's model: pick one it knows or type another (Codex: its model name, OpenCode: provider/model); empty: its default.",
        'de': 'Das Modell des Agenten: eines wählen, das er kennt, oder ein anderes eingeben (Codex: sein Modellname, OpenCode: anbieter/modell); leer: seine Vorgabe.'},
    'How hard the agent thinks (Claude Code: low to max, Codex: minimal to xhigh); empty: its default. OpenCode sets it in its own configuration.': {
        'en': 'How hard the agent thinks (Claude Code: low to max, Codex: minimal to xhigh); empty: its default. OpenCode sets it in its own configuration.',
        'de': 'Wie gründlich der Agent denkt (Claude Code: low bis max, Codex: minimal bis xhigh); leer: seine Vorgabe. OpenCode stellt es in seiner eigenen Konfiguration ein.'},
    'Reload catalogs': {
        'en': 'Reload catalogs',
        'de': 'Kataloge neu laden'},
    'The catalogs load by themselves when the page is first shown; this fetches them again': {
        'en': 'The catalogs load by themselves when the page is first shown; this fetches them again',
        'de': 'Die Kataloge laden von selbst, wenn die Seite zum ersten Mal gezeigt wird; dies holt sie erneut'},
    'Loading the catalogs…': {
        'en': 'Loading the catalogs…',
        'de': 'Die Kataloge werden geladen …'},
    '{count} entries': {
        'en': '{count} entries',
        'de': '{count} Einträge'},
    '{count} CPUs': {
        'en': '{count} CPUs',
        'de': '{count} CPUs'},
    '{example} (this computer: {have})': {
        'en': '{example} (this computer: {have})',
        'de': '{example} (dieser Rechner: {have})'},
    '/dev/ttyUSB0, /dev/kvm, usb:VENDOR:PRODUCT … one per line': {
        'en': '/dev/ttyUSB0, /dev/kvm, usb:VENDOR:PRODUCT … one per line',
        'de': '/dev/ttyUSB0, /dev/kvm, usb:HERSTELLER:PRODUKT … eines pro Zeile'},
    'No devices found on this computer': {
        'en': 'No devices found on this computer',
        'de': 'Keine Geräte auf diesem Rechner gefunden'},
    'Add a device this computer has': {
        'en': 'Add a device this computer has',
        'de': 'Ein Gerät dieses Rechners hinzufügen'},
    'not found on this computer': {
        'en': 'not found on this computer',
        'de': 'auf diesem Rechner nicht gefunden'},
    'This computer has: {devices}': {
        'en': 'This computer has: {devices}',
        'de': 'Dieser Rechner hat: {devices}'},
    'USB serial': {
        'en': 'USB serial',
        'de': 'USB-seriell'},
    'USB serial (CDC ACM)': {
        'en': 'USB serial (CDC ACM)',
        'de': 'USB-seriell (CDC ACM)'},
    'serial': {
        'en': 'serial',
        'de': 'seriell'},
    'KVM (virtual machines)': {
        'en': 'KVM (virtual machines)',
        'de': 'KVM (virtuelle Maschinen)'},
    'camera / video': {
        'en': 'camera / video',
        'de': 'Kamera / Video'},
    'HID': {
        'en': 'HID',
        'de': 'HID'},
    'I²C bus': {
        'en': 'I²C bus',
        'de': 'I²C-Bus'},
    'SPI': {
        'en': 'SPI',
        'de': 'SPI'},
    'GPIO': {
        'en': 'GPIO',
        'de': 'GPIO'},
    'all USB devices': {
        'en': 'all USB devices',
        'de': 'alle USB-Geräte'},
    'TUN/TAP': {
        'en': 'TUN/TAP',
        'de': 'TUN/TAP'},
    'FUSE': {
        'en': 'FUSE',
        'de': 'FUSE'},
    'Local Gitea': {
        'en': 'Local Gitea',
        'de': 'Lokales Gitea'},
    'A row for {url}, its token from the stored secret {name}': {
        'en': 'A row for {url}, its token from the stored secret {name}',
        'de': 'Eine Zeile für {url}, das Token aus dem gespeicherten Geheimnis {name}'},
    'How a token gets here: keep it once with  sandbox-ai secrets set GH_PUSH  (it reads it from standard input; on a desktop without a keyring: SANDBOX_AI_SECRETS=file sandbox-ai secrets set GH_PUSH), then choose "stored secret" and its name. Better still for every project: sandbox-ai git-logins add https://github.com --secret GH_PUSH (the proxy adds it, no token in any sandbox). Publishing to PyPI, npm, crates.io: sandbox-ai publish-logins add pypi --secret PYPI_TOKEN, then start with --publish pypi. Several accounts: sandbox-ai identities, then --identity NAME.': {
        'en': 'How a token gets here: keep it once with  sandbox-ai secrets set GH_PUSH  (it reads it from standard input; on a desktop without a keyring: SANDBOX_AI_SECRETS=file sandbox-ai secrets set GH_PUSH), then choose "stored secret" and its name. Better still for every project: sandbox-ai git-logins add https://github.com --secret GH_PUSH (the proxy adds it, no token in any sandbox). Publishing to PyPI, npm, crates.io: sandbox-ai publish-logins add pypi --secret PYPI_TOKEN, then start with --publish pypi. Several accounts: sandbox-ai identities, then --identity NAME.',
        'de': 'So kommt ein Token hierher: Bewahren Sie es einmal auf mit  sandbox-ai secrets set GH_PUSH  (es liest es von der Standardeingabe; auf einem Desktop ohne Schlüsselbund: SANDBOX_AI_SECRETS=file sandbox-ai secrets set GH_PUSH), dann wählen Sie "gespeichertes Geheimnis" und seinen Namen. Noch besser für jedes Projekt: sandbox-ai git-logins add https://github.com --secret GH_PUSH (der Proxy fügt es ein, kein Token in einer Sandbox). Veröffentlichen bei PyPI, npm, crates.io: sandbox-ai publish-logins add pypi --secret PYPI_TOKEN, dann mit --publish pypi starten. Mehrere Konten: sandbox-ai identities, dann --identity NAME.'},
    'GH_TOKEN  (a variable of the shell the app was started from)': {
        'en': 'GH_TOKEN  (a variable of the shell the app was started from)',
        'de': 'GH_TOKEN  (eine Variable der Shell, aus der die App gestartet wurde)'},
    'GH_PUSH  (sandbox-ai secrets set GH_PUSH)': {
        'en': 'GH_PUSH  (sandbox-ai secrets set GH_PUSH)',
        'de': 'GH_PUSH  (sandbox-ai secrets set GH_PUSH)'},
    'ghp_…  (kept in a private file, never in the project file)': {
        'en': 'ghp_…  (kept in a private file, never in the project file)',
        'de': 'ghp_…  (in einer privaten Datei aufbewahrt, nie in der Projektdatei)'},
    '/home/you/.config/tokens/github  (one line, mode 0600)': {
        'en': '/home/you/.config/tokens/github  (one line, mode 0600)',
        'de': '/home/sie/.config/tokens/github  (eine Zeile, Modus 0600)'},
    'Kept secrets: {names}': {
        'en': 'Kept secrets: {names}',
        'de': 'Gespeicherte Geheimnisse: {names}'},
    'none yet': {
        'en': 'none yet',
        'de': 'noch keine'},
    'Show the log': {
        'en': 'Show the log',
        'de': 'Log anzeigen'},
    'No log yet: {error}': {
        'en': 'No log yet: {error}',
        'de': 'Noch kein Log: {error}'},
    'Log: {path}': {
        'en': 'Log: {path}',
        'de': 'Log: {path}'},
    'Debug mode is on.': {
        'en': 'Debug mode is on.',
        'de': 'Der Debug-Modus ist an.'},
    'Debug mode is off (Settings > Window): only errors and warnings are logged.': {
        'en': 'Debug mode is off (Settings > Window): only errors and warnings are logged.',
        'de': 'Der Debug-Modus ist aus (Einstellungen > Fenster): Nur Fehler und Warnungen werden protokolliert.'},
    'Debug mode: log every step (pages, commands, their times and results)': {
        'en': 'Debug mode: log every step (pages, commands, their times and results)',
        'de': 'Debug-Modus: jeden Schritt protokollieren (Seiten, Befehle, ihre Dauer und Ergebnisse)'},
    'The log: {path} (Help > Show the log)': {
        'en': 'The log: {path} (Help > Show the log)',
        'de': 'Das Log: {path} (Hilfe > Log anzeigen)'},
    'Questions of the running sandboxes': {
        'en': 'Questions of the running sandboxes',
        'de': 'Fragen der laufenden Sandboxen'},
    'A sandbox asks while a dialog is open. Answer here, or close this window and answer on the Approvals page.': {
        'en': 'A sandbox asks while a dialog is open. Answer here, or close this window and answer on the Approvals page.',
        'de': 'Eine Sandbox fragt, während ein Dialog offen ist. Antworten Sie hier, oder schließen Sie dieses Fenster und antworten Sie auf der Seite Freigaben.'},
    'Allow once': {
        'en': 'Allow once',
        'de': 'Einmal erlauben'},
    '(no commit name)': {
        'en': '(no commit name)',
        'de': '(kein Commit-Name)'},
    'Commit address': {
        'en': 'Commit address',
        'de': 'Commit-Adresse'},
    'Commit name': {
        'en': 'Commit name',
        'de': 'Commit-Name'},
    'Commits as': {
        'en': 'Commits as',
        'de': 'Committet als'},
    'Each Git login needs a server (https://HOST, or http://localhost:PORT) and a secret.': {
        'en': 'Each Git login needs a server (https://HOST, or http://localhost:PORT) and a secret.',
        'de': 'Jede Git-Anmeldung braucht einen Server (https://HOST oder http://localhost:PORT) und ein Secret.'},
    'Each kind of publish login once.': {
        'en': 'Each kind of publish login once.',
        'de': 'Jede Art von Veröffentlichungs-Anmeldung nur einmal.'},
    'Edit…': {
        'en': 'Edit…',
        'de': 'Bearbeiten…'},
    'Git logins (the token is a secret of sandbox-ai secrets; the proxy adds it)': {
        'en': 'Git logins (the token is a secret of sandbox-ai secrets; the proxy adds it)',
        'de': 'Git-Anmeldungen (das Token ist ein Secret von sandbox-ai secrets; der Proxy fügt es ein)'},
    'Git servers': {
        'en': 'Git servers',
        'de': 'Git-Server'},
    'Identities (sandbox-ai identities)': {
        'en': 'Identities (sandbox-ai identities)',
        'de': 'Identitäten (sandbox-ai identities)'},
    'Identity': {
        'en': 'Identity',
        'de': 'Identität'},
    'Identity {name}': {
        'en': 'Identity {name}',
        'de': 'Identität {name}'},
    'New identity': {
        'en': 'New identity',
        'de': 'Neue Identität'},
    'Own server (optional)': {
        'en': 'Own server (optional)',
        'de': 'Eigener Server (optional)'},
    'Publish logins (used with --publish KIND)': {
        'en': 'Publish logins (used with --publish KIND)',
        'de': 'Veröffentlichungs-Anmeldungen (genutzt mit --publish ART)'},
    'Publishes to': {
        'en': 'Publishes to',
        'de': 'Veröffentlicht nach'},
    'Remove identity': {
        'en': 'Remove identity',
        'de': 'Identität entfernen'},
    'Remove the identity {name}? Its secrets stay in sandbox-ai secrets.': {
        'en': 'Remove the identity {name}? Its secrets stay in sandbox-ai secrets.',
        'de': 'Die Identität {name} entfernen? Ihre Secrets bleiben in sandbox-ai secrets.'},
    'Saving {name}…': {
        'en': 'Saving {name}…',
        'de': '{name} wird gespeichert…'},
    "The identity's name is lower-case letters, digits, - and _ (not default).": {
        'en': "The identity's name is lower-case letters, digits, - and _ (not default).",
        'de': 'Der Name der Identität besteht aus Kleinbuchstaben, Ziffern, - und _ (nicht default).'},
    'There is an identity {name} already.': {
        'en': 'There is an identity {name} already.',
        'de': 'Es gibt schon eine Identität {name}.'},
    'User': {
        'en': 'User',
        'de': 'Benutzer'},
    'Who a sandbox works as: commit name and address, Git and publish logins under one name; a start uses one with --identity NAME. Without it: default (your git-logins and publish-logins).': {
        'en': 'Who a sandbox works as: commit name and address, Git and publish logins under one name; a start uses one with --identity NAME. Without it: default (your git-logins and publish-logins).',
        'de': 'Als wer eine Sandbox arbeitet: Commit-Name und -Adresse, Git- und Veröffentlichungs-Anmeldungen unter einem Namen; ein Start nutzt eine mit --identity NAME. Ohne: default (deine git-logins und publish-logins).'},
    "a secret's name, e.g. GH_PUSH": {
        'en': "a secret's name, e.g. GH_PUSH",
        'de': 'der Name eines Secrets, z. B. GH_PUSH'},
    'lower-case letters, digits, - and _, e.g. work or c0decave': {
        'en': 'lower-case letters, digits, - and _, e.g. work or c0decave',
        'de': 'Kleinbuchstaben, Ziffern, - und _, z. B. work oder c0decave'},
    'the commit address, e.g. you@users.noreply.github.com': {
        'en': 'the commit address, e.g. you@users.noreply.github.com',
        'de': 'die Commit-Adresse, z. B. du@users.noreply.github.com'},
    'the commit name, e.g. Ada Lovelace': {
        'en': 'the commit name, e.g. Ada Lovelace',
        'de': 'der Commit-Name, z. B. Ada Lovelace'},
    '{name} saved.': {
        'en': '{name} saved.',
        'de': '{name} gespeichert.'},
    'Error': {
        'en': 'Error',
        'de': 'Fehler'},
    'Warning': {
        'en': 'Warning',
        'de': 'Warnung'},
    'Information': {
        'en': 'Information',
        'de': 'Information'},
    'Open approvals': {
        'en': 'Open approvals',
        'de': 'Freigaben öffnen'},
    'Notifications are shown': {
        'en': 'Notifications are shown',
        'de': 'Benachrichtigungen erscheinen'},
    "in the app's theme": {
        'en': "in the app's theme",
        'de': 'im Design der App'},
    "as the desktop's notifications": {
        'en': "as the desktop's notifications",
        'de': 'als Benachrichtigungen des Desktops'},
    "In the app's theme: a popup at the corner of the screen in the theme's colours (warnings in its warning colour, errors in its error colour; errors stay until closed). Where a window cannot place itself (Wayland), the desktop's notification is used.": {
        'en': "In the app's theme: a popup at the corner of the screen in the theme's colours (warnings in its warning colour, errors in its error colour; errors stay until closed). Where a window cannot place itself (Wayland), the desktop's notification is used.",
        'de': 'Im Design der App: ein Hinweis in der Ecke des Bildschirms in den Farben des Designs (Warnungen in seiner Warnfarbe, Fehler in seiner Fehlerfarbe; Fehler bleiben, bis sie geschlossen werden). Wo ein Fenster sich nicht selbst platzieren kann (Wayland), wird die Benachrichtigung des Desktops genutzt.'},
    # >>> help area, (?) buttons, tours through a task, board drag and drop (feat/board-help-tours) >>>
    'What is this? {title}': {
        'en': 'What is this? {title}',
        'de': 'Was ist das? {title}'},
    'Add to the board': {
        'en': 'Add to the board',
        'de': 'Zur Tafel hinzufügen'},
    'What is this?': {
        'en': 'What is this?',
        'de': 'Was ist das?'},
    'Drag a part onto the board, or double-click it': {
        'en': 'Drag a part onto the board, or double-click it',
        'de': 'Ziehen Sie ein Teil auf die Tafel oder doppelklicken Sie es'},
    'Drag a part onto the board, or double-click it.': {
        'en': 'Drag a part onto the board, or double-click it.',
        'de': 'Ziehen Sie ein Teil auf die Tafel oder doppelklicken Sie es.'},
    'Drop: {names}': {
        'en': 'Drop: {names}',
        'de': 'Ablegen: {names}'},
    "{path} is a preset of sandbox-ai itself: keep it in {folder} and it is listed under sandbox-ai's presets in the gallery.": {
        'en': "{path} is a preset of sandbox-ai itself: keep it in {folder} and it is listed under sandbox-ai's presets in the gallery.",
        'de': '{path} ist ein Preset von sandbox-ai selbst: Legen Sie es in {folder} ab, dann steht es in der Galerie unter den Presets von sandbox-ai.'},
    '{name} is on the board already': {
        'en': '{name} is on the board already',
        'de': '{name} ist schon auf der Tafel'},
    'Drop to add {name}': {
        'en': 'Drop to add {name}',
        'de': 'Ablegen fügt {name} hinzu'},
    'Use it for {name}': {
        'en': 'Use it for {name}',
        'de': 'Für {name} verwenden'},
    'Open the project {path}': {
        'en': 'Open the project {path}',
        'de': 'Das Projekt {path} öffnen'},
    'Add as workspace': {
        'en': 'Add as workspace',
        'de': 'Als Arbeitsordner hinzufügen'},
    'Add as read-only mount': {
        'en': 'Add as read-only mount',
        'de': 'Als nur lesbare Einbindung hinzufügen'},
    'Add as writable mount': {
        'en': 'Add as writable mount',
        'de': 'Als beschreibbare Einbindung hinzufügen'},
    'Import as my preset and build the board from it': {
        'en': 'Import as my preset and build the board from it',
        'de': 'Als meine Vorlage importieren und die Tafel daraus bauen'},
    'Import as my preset': {
        'en': 'Import as my preset',
        'de': 'Als meine Vorlage importieren'},
    'Imported as your preset {name}: it is under My presets in the gallery.': {
        'en': 'Imported as your preset {name}: it is under My presets in the gallery.',
        'de': 'Als Ihre Vorlage {name} importiert: Sie steht in der Galerie unter Meine Vorlagen.'},
    'On the board already: a board has one.': {
        'en': 'On the board already: a board has one.',
        'de': 'Schon auf der Tafel: Eine Tafel hat nur eines.'},
    'Help': {
        'en': 'Help',
        'de': 'Hilfe'},
    'Everything explained again, in English and German: every page and feature of the app and the ideas behind sandbox-ai, searchable, with tours through whole tasks. Each (?) in the app leads here.': {
        'en': 'Everything explained again, in English and German: every page and feature of the app and the ideas behind sandbox-ai, searchable, with tours through whole tasks. Each (?) in the app leads here.',
        'de': 'Alles noch einmal erklärt, auf Englisch und Deutsch: jede Seite und Funktion der App und die Ideen hinter sandbox-ai, durchsuchbar, mit Touren durch ganze Aufgaben. Jedes (?) in der App führt hierher.'},
    'Search for preset, then start the tour that builds and saves a preset of your own.': {
        'en': 'Search for preset, then start the tour that builds and saves a preset of your own.',
        'de': 'Suchen Sie nach Vorlage und starten Sie dann die Tour, die eine eigene Vorlage baut und speichert.'},
    '{tour} · {number} of {count}': {
        'en': '{tour} · {number} of {count}',
        'de': '{tour} · {number} von {count}'},
    '✔ Done: go on with Next.': {
        'en': '✔ Done: go on with Next.',
        'de': '✔ Erledigt: weiter mit Weiter.'},
    'More in Help': {
        'en': 'More in Help',
        'de': 'Mehr in der Hilfe'},
    'Tour of the page {page}': {
        'en': 'Tour of the page {page}',
        'de': 'Tour der Seite {page}'},
    'Search the help, in English or German': {
        'en': 'Search the help, in English or German',
        'de': 'Die Hilfe durchsuchen, auf Englisch oder Deutsch'},
    'Language of the app': {
        'en': 'Language of the app',
        'de': 'Sprache der App'},
    'Command line: {cli}': {
        'en': 'Command line: {cli}',
        'de': 'Befehlszeile: {cli}'},
    'Nothing matches {text}.': {
        'en': 'Nothing matches {text}.',
        'de': 'Nichts passt zu {text}.'},
    'The app': {
        'en': 'The app',
        'de': 'Die App'},
    'Concepts': {
        'en': 'Concepts',
        'de': 'Konzepte'},
    'Start: {tour}': {
        'en': 'Start: {tour}',
        'de': 'Starten: {tour}'},
    'All tours': {
        'en': 'All tours',
        'de': 'Alle Touren'},
    'Tours': {
        'en': 'Tours',
        'de': 'Touren'},
    'Tours of the pages': {
        'en': 'Tours of the pages',
        'de': 'Touren der Seiten'},
    'See also': {
        'en': 'See also',
        'de': 'Siehe auch'},
    'Help: every topic, in English and German': {
        'en': 'Help: every topic, in English and German',
        'de': 'Hilfe: jedes Thema, auf Englisch und Deutsch'},
    'Tours through a task': {
        'en': 'Tours through a task',
        'de': 'Touren durch eine Aufgabe'},
    # <<< help area, (?) buttons, tours through a task, board drag and drop <<<,
    'unreachable': {
        'en': 'unreachable',
        'de': 'nicht erreichbar'},
    '{version}, update: {available}': {
        'en': '{version}, update: {available}',
        'de': '{version}, Update: {available}'},
    '{version}, up to date': {
        'en': '{version}, up to date',
        'de': '{version}, aktuell'},
    '{path} could be read by others (mode {was}), which ssh refuses: made private ({now})': {
        'en': '{path} could be read by others (mode {was}), which ssh refuses: made private ({now})',
        'de': '{path} war für andere lesbar (Modus {was}), was ssh ablehnt: privat gemacht ({now})'},
    '{host}: not updated: {error}': {
        'en': '{host}: not updated: {error}',
        'de': '{host}: nicht aktualisiert: {error}'},
    '{host}: {before} -> {after}': {
        'en': '{host}: {before} -> {after}',
        'de': '{host}: {before} -> {after}'},
    '{host}: {version}, up to date': {
        'en': '{host}: {version}, up to date',
        'de': '{host}: {version}, aktuell'},
    'Update': {
        'en': 'Update',
        'de': 'Aktualisieren'},
    'Update all': {
        'en': 'Update all',
        'de': 'Alle aktualisieren'},
    'Check versions': {
        'en': 'Check versions',
        'de': 'Versionen prüfen'},
    'sandbox-ai update on this host (its own signed update; sudo for a system installation)': {
        'en': 'sandbox-ai update on this host (its own signed update; sudo for a system installation)',
        'de': 'sandbox-ai update auf diesem Host (sein eigenes signiertes Update; sudo für eine Systeminstallation)'},
    'sandbox-ai update on every host of the list, one after the other': {
        'en': 'sandbox-ai update on every host of the list, one after the other',
        'de': 'sandbox-ai update auf jedem Host der Liste, einer nach dem anderen'},
    "Update or Update all: each host's own sandbox-ai update, signed, as install.sh installed it there.": {
        'en': "Update or Update all: each host's own sandbox-ai update, signed, as install.sh installed it there.",
        'de': 'Aktualisieren oder Alle aktualisieren: das eigene sandbox-ai-Update jedes Hosts, signiert, so wie install.sh es dort installiert hat.'},
    'Updates': {
        'en': 'Updates',
        'de': 'Updates'},
    'This sandbox-ai cannot update hosts yet: update it.': {
        'en': 'This sandbox-ai cannot update hosts yet: update it.',
        'de': 'Dieses sandbox-ai kann Hosts noch nicht aktualisieren: bitte aktualisieren.'},
    'Asking the hosts for their versions…': {
        'en': 'Asking the hosts for their versions…',
        'de': 'Frage die Hosts nach ihren Versionen…'},
    'Updates for: {hosts}': {
        'en': 'Updates for: {hosts}',
        'de': 'Updates für: {hosts}'},
    'Every host answered is up to date.': {
        'en': 'Every host answered is up to date.',
        'de': 'Jeder Host, der geantwortet hat, ist aktuell.'},
    'This machine updates on the page System (or: sandbox-ai update).': {
        'en': 'This machine updates on the page System (or: sandbox-ai update).',
        'de': 'Diese Maschine wird auf der Seite System aktualisiert (oder: sandbox-ai update).'},
    'Updating {host} ({index} of {count})…': {
        'en': 'Updating {host} ({index} of {count})…',
        'de': 'Aktualisiere {host} ({index} von {count})…'},
    'The installation on {host} belongs to root. Update it with sudo?': {
        'en': 'The installation on {host} belongs to root. Update it with sudo?',
        'de': 'Die Installation auf {host} gehört root. Mit sudo aktualisieren?'},
    'Update with sudo': {
        'en': 'Update with sudo',
        'de': 'Mit sudo aktualisieren'},
    '{host}: update': {
        'en': '{host}: update',
        'de': '{host}: Update'},
    '  {host}: the update runs in a terminal tab, where sudo asks for the password': {
        'en': '  {host}: the update runs in a terminal tab, where sudo asks for the password',
        'de': '  {host}: das Update läuft in einem Terminal-Tab, dort fragt sudo nach dem Passwort'},
    '{count} hosts: {updated} updated, {current} up to date, {failed} not updated': {
        'en': '{count} hosts: {updated} updated, {current} up to date, {failed} not updated',
        'de': '{count} Hosts: {updated} aktualisiert, {current} aktuell, {failed} nicht aktualisiert'},
    'Attach {name} to…': {
        'en': 'Attach {name} to…',
        'de': '{name} verbinden mit…'},
    'uses it': {
        'en': 'uses it',
        'de': 'nutzt ihn'},
    'Restart it now if it runs (the agent continues its conversation)': {
        'en': 'Restart it now if it runs (the agent continues its conversation)',
        'de': 'Jetzt neu starten, falls er läuft (der Agent setzt sein Gespräch fort)'},
    "A running sandbox cannot take a new MCP server while it runs: its agent reads its servers when it starts, and the server's socket is mounted at the start. The server is kept in the start's options: without a restart it is there from its next start on.": {
        'en': "A running sandbox cannot take a new MCP server while it runs: its agent reads its servers when it starts, and the server's socket is mounted at the start. The server is kept in the start's options: without a restart it is there from its next start on.",
        'de': 'Eine laufende Sandbox kann keinen neuen MCP-Server aufnehmen, solange sie läuft: ihr Agent liest seine Server beim Start, und der Socket des Servers wird beim Start eingehängt. Der Server bleibt in den Optionen des Starts: ohne Neustart ist er ab dem nächsten Start da.'},
    'Serves': {
        'en': 'Serves',
        'de': 'Bedient'},
    'Attach to…': {
        'en': 'Attach to…',
        'de': 'Verbinden mit…'},
    'Connect the server to an agent start (sandbox-ai mcp attach)': {
        'en': 'Connect the server to an agent start (sandbox-ai mcp attach)',
        'de': 'Den Server mit einem Agenten-Start verbinden (sandbox-ai mcp attach)'},
    'Select a server first.': {
        'en': 'Select a server first.',
        'de': 'Bitte zuerst einen Server auswählen.'},
    'No kept agent start to attach it to: start one (a run without a kept start takes no new server).': {
        'en': 'No kept agent start to attach it to: start one (a run without a kept start takes no new server).',
        'de': 'Kein behaltener Agenten-Start, mit dem er verbunden werden kann: bitte einen starten (ein Lauf ohne behaltenen Start nimmt keinen neuen Server auf).'},
    'Agent start': {
        'en': 'Agent start',
        'de': 'Agenten-Start'},
    # <<< help area, (?) buttons, tours through a task, board drag and drop <<<
    "The board uses MCP servers the project defines ({names}). The launcher knows them only from the project's trusted .sandbox-ai.json. {why} It is written and trusted now, then the sandbox starts.": {
        'en': "The board uses MCP servers the project defines ({names}). The launcher knows them only from the project's trusted .sandbox-ai.json. {why} It is written and trusted now, then the sandbox starts.",
        'de': 'Das Board nutzt MCP-Server, die das Projekt selbst definiert ({names}). Der Launcher kennt sie nur aus der vertrauten .sandbox-ai.json des Projekts. {why} Sie wird jetzt geschrieben und vertraut, dann startet die Sandbox.'},
    'The board uses MCP servers the project defines ({names}); they are written into the project file, which needs a workspace first.': {
        'en': 'The board uses MCP servers the project defines ({names}); they are written into the project file, which needs a workspace first.',
        'de': 'Das Board nutzt MCP-Server, die das Projekt selbst definiert ({names}); sie stehen in der Projektdatei, und die braucht zuerst einen Arbeitsordner.'},
    '{path} does not hold the board after it was written; look at it, then Apply again.': {
        'en': '{path} does not hold the board after it was written; look at it, then Apply again.',
        'de': '{path} enthält nach dem Schreiben nicht das Board; sehen Sie sie an und wenden Sie dann erneut an.'},
    'The project file is not written yet.': {
        'en': 'The project file is not written yet.',
        'de': 'Die Projektdatei ist noch nicht geschrieben.'},
    'The project file is not trusted (or changed since it was trusted).': {
        'en': 'The project file is not trusted (or changed since it was trusted).',
        'de': 'Der Projektdatei wird nicht vertraut (oder sie hat sich seitdem geändert).'},
    'The project file differs from the board.': {
        'en': 'The project file differs from the board.',
        'de': 'Die Projektdatei weicht vom Board ab.'},
    'The project file cannot be read.': {
        'en': 'The project file cannot be read.',
        'de': 'Die Projektdatei kann nicht gelesen werden.'},
    "The board uses MCP servers the project defines ({names}). The launcher knows them only from the project's trusted .sandbox-ai.json, which was not written. Apply it (it is shown first), then start.": {
        'en': "The board uses MCP servers the project defines ({names}). The launcher knows them only from the project's trusted .sandbox-ai.json, which was not written. Apply it (it is shown first), then start.",
        'de': 'Das Board nutzt MCP-Server, die das Projekt selbst definiert ({names}). Der Launcher kennt sie nur aus der vertrauten .sandbox-ai.json des Projekts, und die wurde nicht geschrieben. Wenden Sie sie an (sie wird zuerst gezeigt), dann starten Sie.'},
    'The project file was not written, so the start that needs it did not happen.': {
        'en': 'The project file was not written, so the start that needs it did not happen.',
        'de': 'Die Projektdatei wurde nicht geschrieben, darum fand der Start, der sie braucht, nicht statt.'},
    'Not attached': {
        'en': 'Not attached',
        'de': 'Nicht verbunden'},
    "inside sandbox-ai's own folder ({path})": {
        'en': "inside sandbox-ai's own folder ({path})",
        'de': 'im eigenen Ordner von sandbox-ai ({path})'},
    'Choose a folder (a new one is created).': {
        'en': 'Choose a folder (a new one is created).',
        'de': 'Wählen Sie einen Ordner (ein neuer wird angelegt).'},
    'Give the full path of the folder (it starts with / or ~).': {
        'en': 'Give the full path of the folder (it starts with / or ~).',
        'de': 'Geben Sie den vollständigen Pfad des Ordners an (er beginnt mit / oder ~).'},
    '{path} exists and is not a folder.': {
        'en': '{path} exists and is not a folder.',
        'de': '{path} existiert und ist kein Ordner.'},
    'Not created: {path} is {why}.': {
        'en': 'Not created: {path} is {why}.',
        'de': 'Nicht angelegt: {path} ist {why}.'},
    'Create the folder': {
        'en': 'Create the folder',
        'de': 'Ordner anlegen'},
    '{path} does not exist yet. Create it (and the folders above it that are missing) as the new project folder?': {
        'en': '{path} does not exist yet. Create it (and the folders above it that are missing) as the new project folder?',
        'de': '{path} existiert noch nicht. Als neuen Projektordner anlegen (samt fehlender übergeordneter Ordner)?'},
    'Choose a folder that exists, or agree to create it.': {
        'en': 'Choose a folder that exists, or agree to create it.',
        'de': 'Wählen Sie einen vorhandenen Ordner oder stimmen Sie dem Anlegen zu.'},
    '{path} could not be created: {error}': {
        'en': '{path} could not be created: {error}',
        'de': '{path} konnte nicht angelegt werden: {error}'},
    'These end with the app: {list}. Sessions keep running (attach again from Terminals or with sandbox-ai attach NAME). Quit?': {
        'en': 'These end with the app: {list}. Sessions keep running (attach again from Terminals or with sandbox-ai attach NAME). Quit?',
        'de': 'Diese enden mit der App: {list}. Sitzungen laufen weiter (wieder verbinden über Terminals oder mit sandbox-ai attach NAME). Beenden?'},
    'End them and quit': {
        'en': 'End them and quit',
        'de': 'Beenden und schließen'},
    'Show the (?) help buttons (the Help page, the tours and F1 stay)': {
        'en': 'Show the (?) help buttons (the Help page, the tours and F1 stay)',
        'de': 'Die (?)-Hilfeknöpfe zeigen (die Hilfe-Seite, die Touren und F1 bleiben)'},
    'Fine': {
        'en': 'Fine',
        'de': 'In Ordnung'},
    'Problem': {
        'en': 'Problem',
        'de': 'Problem'},
    'Not checked': {
        'en': 'Not checked',
        'de': 'Nicht geprüft'},
    'Engine (Podman, Bubblewrap, images)': {
        'en': 'Engine (Podman, Bubblewrap, images)',
        'de': 'Engine (Podman, Bubblewrap, Images)'},
    'Agents (logins, kits, plugins)': {
        'en': 'Agents (logins, kits, plugins)',
        'de': 'Agenten (Anmeldungen, Kits, Plugins)'},
    'Project and security mode': {
        'en': 'Project and security mode',
        'de': 'Projekt und Sicherheitsmodus'},
    'Everything needed is in place.': {
        'en': 'Everything needed is in place.',
        'de': 'Alles Nötige ist vorhanden.'},
    'The doctor found nothing to check.': {
        'en': 'The doctor found nothing to check.',
        'de': 'Der Doctor hat nichts zu prüfen gefunden.'},
    '{failed} problem(s), {warned} warning(s): each says how to fix it.': {
        'en': '{failed} problem(s), {warned} warning(s): each says how to fix it.',
        'de': '{failed} Problem(e), {warned} Warnung(en): Bei jedem steht, wie es sich beheben lässt.'},
    'Not checked yet: press Check.': {
        'en': 'Not checked yet: press Check.',
        'de': 'Noch nicht geprüft: Drücken Sie auf Prüfen.'},
    'Only problems and warnings': {
        'en': 'Only problems and warnings',
        'de': 'Nur Probleme und Warnungen'},
    'Copy the fix command': {
        'en': 'Copy the fix command',
        'de': 'Befehl zum Beheben kopieren'},
    'Copies the command of the selected check; run it in a terminal yourself': {
        'en': 'Copies the command of the selected check; run it in a terminal yourself',
        'de': 'Kopiert den Befehl der gewählten Prüfung; führen Sie ihn selbst in einem Terminal aus'},
    'What was found': {
        'en': 'What was found',
        'de': 'Was gefunden wurde'},
    '{failed} problem(s), {warned} warning(s)': {
        'en': '{failed} problem(s), {warned} warning(s)',
        'de': '{failed} Problem(e), {warned} Warnung(en)'},
    'all fine': {
        'en': 'all fine',
        'de': 'alles in Ordnung'},
    'Command: {command} (double-click copies it)': {
        'en': 'Command: {command} (double-click copies it)',
        'de': 'Befehl: {command} (Doppelklick kopiert ihn)'},
    '{version} now (alias; the installed Claude Code decides)': {
        'en': '{version} now (alias; the installed Claude Code decides)',
        'de': 'derzeit {version} (Alias; das installierte Claude Code entscheidet)'},
    'Git logins (sandbox-ai git-logins)': {
        'en': 'Git logins (sandbox-ai git-logins)',
        'de': 'Git-Logins (sandbox-ai git-logins)'},
    'Publish logins (sandbox-ai publish-logins)': {
        'en': 'Publish logins (sandbox-ai publish-logins)',
        'de': 'Veröffentlichungs-Logins (sandbox-ai publish-logins)'},
    'Agent login': {
        'en': 'Agent login',
        'de': 'Login eines Agenten'},
    'Git login': {
        'en': 'Git login',
        'de': 'Git-Login'},
    'Publish login': {
        'en': 'Publish login',
        'de': 'Veröffentlichungs-Login'},
    'The connection proxy of every sandbox logs in to these Git servers on the wire (clone, fetch, push); no token enters a sandbox. The token is the secret named.': {
        'en': 'The connection proxy of every sandbox logs in to these Git servers on the wire (clone, fetch, push); no token enters a sandbox. The token is the secret named.',
        'de': 'Der Verbindungs-Proxy jeder Sandbox meldet sich unterwegs bei diesen Git-Servern an (clone, fetch, push); kein Token gelangt in eine Sandbox. Das Token ist das genannte Geheimnis.'},
    'Package registries and forges a start may publish to with --publish KIND; the proxy adds the token on the wire and asks before every upload.': {
        'en': 'Package registries and forges a start may publish to with --publish KIND; the proxy adds the token on the wire and asks before every upload.',
        'de': 'Paket-Registries und Forges, in die ein Start mit --publish ART veröffentlichen darf; der Proxy fügt das Token unterwegs ein und fragt vor jedem Hochladen.'},
    'Agent logins': {
        'en': 'Agent logins',
        'de': 'Logins der Agenten'},
    'Secrets': {
        'en': 'Secrets',
        'de': 'Geheimnisse'},
    'Git logins': {
        'en': 'Git logins',
        'de': 'Git-Logins'},
    'Publish logins': {
        'en': 'Publish logins',
        'de': 'Veröffentlichungs-Logins'},
    'Identities': {
        'en': 'Identities',
        'de': 'Identitäten'},
    'ready': {
        'en': 'ready',
        'de': 'bereit'},
    'secret {name} missing': {
        'en': 'secret {name} missing',
        'de': 'Geheimnis {name} fehlt'},
    'Working…': {
        'en': 'Working…',
        'de': 'Wird ausgeführt…'},
    'New Git login': {
        'en': 'New Git login',
        'de': 'Neuer Git-Login'},
    'New publish login': {
        'en': 'New publish login',
        'de': 'Neuer Veröffentlichungs-Login'},
    'Secret (its name)': {
        'en': 'Secret (its name)',
        'de': 'Geheimnis (sein Name)'},
    'Overview': {
        'en': 'Overview',
        'de': 'Übersicht'},
    'Not done: {error}': {
        'en': 'Not done: {error}',
        'de': 'Nicht erledigt: {error}'},
    'Keep the token first on the Secrets tab; only its name is written here.': {
        'en': 'Keep the token first on the Secrets tab; only its name is written here.',
        'de': 'Bewahren Sie das Token zuerst im Reiter Geheimnisse auf; hier wird nur sein Name eingetragen.'},
    "Give the server's address.": {
        'en': "Give the server's address.",
        'de': 'Geben Sie die Adresse des Servers an.'},
    'Everything configured, with its state. Click a row to open its tab.': {
        'en': 'Everything configured, with its state. Click a row to open its tab.',
        'de': 'Alles Eingerichtete mit seinem Zustand. Ein Klick auf eine Zeile öffnet ihren Reiter.'},
    'Where / used by': {
        'en': 'Where / used by',
        'de': 'Wo / genutzt von'},
    'Used by': {
        'en': 'Used by',
        'de': 'Genutzt von'},
    'Git login {name}': {
        'en': 'Git login {name}',
        'de': 'Git-Login {name}'},
    'publish login {name}': {
        'en': 'publish login {name}',
        'de': 'Veröffentlichungs-Login {name}'},
    '{count} configured, {problems} need attention.': {
        'en': '{count} configured, {problems} need attention.',
        'de': '{count} eingerichtet, {problems} brauchen Aufmerksamkeit.'},
    'Nothing configured yet: log in to an agent, keep a secret.': {
        'en': 'Nothing configured yet: log in to an agent, keep a secret.',
        'de': 'Noch nichts eingerichtet: Melden Sie sich bei einem Agenten an, bewahren Sie ein Geheimnis auf.'},
    'Remove the Git login for {name}? Its secret stays.': {
        'en': 'Remove the Git login for {name}? Its secret stays.',
        'de': 'Den Git-Login für {name} entfernen? Sein Geheimnis bleibt.'},
    'Remove the publish login for {name}? Its secret stays.': {
        'en': 'Remove the publish login for {name}? Its secret stays.',
        'de': 'Den Veröffentlichungs-Login für {name} entfernen? Sein Geheimnis bleibt.'},
    'kept': {
        'en': 'kept',
        'de': 'aufbewahrt'},
    'configured': {
        'en': 'configured',
        'de': 'eingerichtet'},
    'identity {name}': {
        'en': 'identity {name}',
        'de': 'Identität {name}'},
    '{where}, secret {secret}': {
        'en': '{where}, secret {secret}',
        'de': '{where}, Geheimnis {secret}'},
    'Names separated by spaces. *.DOMAIN opens every name below DOMAIN, each as it is looked up (not for shared hosting such as *.github.io, nor public suffixes).': {
        'en': 'Names separated by spaces. *.DOMAIN opens every name below DOMAIN, each as it is looked up (not for shared hosting such as *.github.io, nor public suffixes).',
        'de': 'Namen durch Leerzeichen getrennt. *.DOMAIN öffnet jeden Namen unter DOMAIN, jeweils wenn er nachgeschlagen wird (nicht für geteiltes Hosting wie *.github.io und nicht für öffentliche Suffixe).'},
    'Add workspace to {run}': {
        'en': 'Add workspace to {run}',
        'de': 'Workspace zu {run} hinzufügen'},
    '/path/to/a/folder': {
        'en': '/path/to/a/folder',
        'de': '/pfad/zu/einem/ordner'},
    'Choose…': {
        'en': 'Choose…',
        'de': 'Wählen…'},
    'Read-only (the agent cannot change it)': {
        'en': 'Read-only (the agent cannot change it)',
        'de': 'Nur lesen (der Agent kann ihn nicht ändern)'},
    'It appears at /workspaces/NAME until the sandbox ends; a session keeps it for a restart unless it is read-only. sandbox-ai checks it as at a start: private places, package stores and the policy are refused.': {
        'en': 'It appears at /workspaces/NAME until the sandbox ends; a session keeps it for a restart unless it is read-only. sandbox-ai checks it as at a start: private places, package stores and the policy are refused.',
        'de': 'Er erscheint unter /workspaces/NAME, bis die Sandbox endet; eine Sitzung behält ihn für einen Neustart, außer er ist nur lesbar. sandbox-ai prüft ihn wie bei einem Start: private Orte, Paketspeicher und die Richtlinie werden abgelehnt.'},
    'Folder for the sandbox': {
        'en': 'Folder for the sandbox',
        'de': 'Ordner für die Sandbox'},
    'Add workspace…': {
        'en': 'Add workspace…',
        'de': 'Workspace hinzufügen…'},
    'Remove workspace…': {
        'en': 'Remove workspace…',
        'de': 'Workspace entfernen…'},
    'Take back a workspace added during the run (sandbox-ai workspace remove)': {
        'en': 'Take back a workspace added during the run (sandbox-ai workspace remove)',
        'de': 'Einen während des Laufs hinzugefügten Workspace zurücknehmen (sandbox-ai workspace remove)'},
    'This sandbox-ai version cannot add workspaces during a run.': {
        'en': 'This sandbox-ai version cannot add workspaces during a run.',
        'de': 'Diese sandbox-ai-Version kann während eines Laufs keine Workspaces hinzufügen.'},
    'Workspaces added during the run: {list}': {
        'en': 'Workspaces added during the run: {list}',
        'de': 'Während des Laufs hinzugefügte Workspaces: {list}'},
    'No workspace added during the run.': {
        'en': 'No workspace added during the run.',
        'de': 'Kein Workspace während des Laufs hinzugefügt.'},
    'Give the folder as an absolute path.': {
        'en': 'Give the folder as an absolute path.',
        'de': 'Gib den Ordner als absoluten Pfad an.'},
    'Add workspace': {
        'en': 'Add workspace',
        'de': 'Workspace hinzufügen'},
    'Let {run} change {path} until it ends? The agent can write, delete and run there.': {
        'en': 'Let {run} change {path} until it ends? The agent can write, delete and run there.',
        'de': '{run} {path} ändern lassen, bis sie endet? Der Agent kann dort schreiben, löschen und ausführen.'},
    'Remove workspace': {
        'en': 'Remove workspace',
        'de': 'Workspace entfernen'},
    'No workspace was added to {run} during its run (those of the start stay).': {
        'en': 'No workspace was added to {run} during its run (those of the start stay).',
        'de': 'Zu {run} wurde während des Laufs kein Workspace hinzugefügt (die des Starts bleiben).'},
    'Detach': {
        'en': 'Detach',
        'de': 'Lösen'},
    '({count} on other hosts)': {
        'en': '({count} on other hosts)',
        'de': '({count} auf anderen Hosts)'},
    'A device is a path under /dev/.': {
        'en': 'A device is a path under /dev/.',
        'de': 'Ein Gerät ist ein Pfad unter /dev/.'},
    'Add device on {host}': {
        'en': 'Add device on {host}',
        'de': 'Gerät auf {host} hinzufügen'},
    'Add device to {run} on {host}': {
        'en': 'Add device to {run} on {host}',
        'de': 'Gerät zu {run} auf {host} hinzufügen'},
    'Add workspace on {host}': {
        'en': 'Add workspace on {host}',
        'de': 'Workspace auf {host} hinzufügen'},
    'Add workspace to {run} on {host}': {
        'en': 'Add workspace to {run} on {host}',
        'de': 'Workspace zu {run} auf {host} hinzufügen'},
    'All hosts': {
        'en': 'All hosts',
        'de': 'Alle Hosts'},
    'Allow for {run} on {host}': {
        'en': 'Allow for {run} on {host}',
        'de': 'Für {run} auf {host} erlauben'},
    'Asked on {host}: the answer goes there, over SSH.': {
        'en': 'Asked on {host}: the answer goes there, over SSH.',
        'de': 'Gefragt auf {host}: Die Antwort geht dorthin, über SSH.'},
    'Delete session {name} and its HOME on this machine? This cannot be undone.': {
        'en': 'Delete session {name} and its HOME on this machine? This cannot be undone.',
        'de': 'Die Sitzung {name} und ihr HOME auf diesem Rechner löschen? Das lässt sich nicht rückgängig machen.'},
    'Folder on {host}': {
        'en': 'Folder on {host}',
        'de': 'Ordner auf {host}'},
    'Give the folder as an absolute path, or its name under /workspaces.': {
        'en': 'Give the folder as an absolute path, or its name under /workspaces.',
        'de': 'Gib den Ordner als absoluten Pfad an oder seinen Namen unter /workspaces.'},
    'Give {path} of {host} to {run} until it ends? Devices weaken the isolation; sandbox-ai there checks the device as at a start.': {
        'en': 'Give {path} of {host} to {run} until it ends? Devices weaken the isolation; sandbox-ai there checks the device as at a start.',
        'de': '{path} von {host} an {run} geben, bis sie endet? Geräte schwächen die Isolation; sandbox-ai dort prüft das Gerät wie bei einem Start.'},
    'If you did not make these changes, check them before it starts again ({command}).': {
        'en': 'If you did not make these changes, check them before it starts again ({command}).',
        'de': 'Wenn diese Änderungen nicht von dir sind, prüfe sie, bevor sie wieder startet ({command}).'},
    'Its HOME has more files than were checked: not every change is named.': {
        'en': 'Its HOME has more files than were checked: not every change is named.',
        'de': 'Ihr HOME hat mehr Dateien, als geprüft wurden: Nicht jede Änderung ist genannt.'},
    'Its connection log is on {host} (sandbox-ai there).': {
        'en': 'Its connection log is on {host} (sandbox-ai there).',
        'de': 'Ihr Verbindungsprotokoll liegt auf {host} (sandbox-ai dort).'},
    'Let {run} change {path} on {host} until it ends? The agent can write, delete and run there.': {
        'en': 'Let {run} change {path} on {host} until it ends? The agent can write, delete and run there.',
        'de': '{run} {path} auf {host} ändern lassen, bis sie endet? Der Agent kann dort schreiben, löschen und ausführen.'},
    'Limits of {run} on {host}': {
        'en': 'Limits of {run} on {host}',
        'de': 'Grenzen von {run} auf {host}'},
    'Memory pressure from {size} on (memory.high): set at the start.': {
        'en': 'Memory pressure from {size} on (memory.high): set at the start.',
        'de': 'Speicherdruck ab {size} (memory.high): beim Start festgelegt.'},
    'Mount a folder of its host into the selected sandbox until it ends (sandbox-ai workspace add)': {
        'en': 'Mount a folder of its host into the selected sandbox until it ends (sandbox-ai workspace add)',
        'de': 'Einen Ordner ihres Hosts in die gewählte Sandbox einhängen, bis sie endet (sandbox-ai workspace add)'},
    'Only http and https pages are opened here.': {
        'en': 'Only http and https pages are opened here.',
        'de': 'Hier werden nur http- und https-Seiten geöffnet.'},
    'Open questions of the running sandboxes, on every host': {
        'en': 'Open questions of the running sandboxes, on every host',
        'de': 'Offene Fragen der laufenden Sandboxen, auf allen Hosts'},
    'Remove device from {run} on {host}': {
        'en': 'Remove device from {run} on {host}',
        'de': 'Gerät von {run} auf {host} entfernen'},
    'Remove host': {
        'en': 'Remove host',
        'de': 'Host entfernen'},
    'Remove workspace from {run} on {host}': {
        'en': 'Remove workspace from {run} on {host}',
        'de': 'Workspace von {run} auf {host} entfernen'},
    'Remove {host} from the list of hosts? Its sandbox-ai and its starts stay there; this app and sandbox-ai web no longer reach it (its approvals are no longer answered from here).': {
        'en': 'Remove {host} from the list of hosts? Its sandbox-ai and its starts stay there; this app and sandbox-ai web no longer reach it (its approvals are no longer answered from here).',
        'de': '{host} aus der Liste der Hosts entfernen? Sein sandbox-ai und seine Starts bleiben dort; diese App und sandbox-ai web erreichen ihn nicht mehr (seine Freigaben werden von hier nicht mehr beantwortet).'},
    'Resume it anyway?': {
        'en': 'Resume it anyway?',
        'de': 'Trotzdem fortsetzen?'},
    'Resume {name}': {
        'en': 'Resume {name}',
        'de': '{name} fortsetzen'},
    'Resume {name} on {host}': {
        'en': 'Resume {name} on {host}',
        'de': '{name} auf {host} fortsetzen'},
    'Revoke for {run} on {host}': {
        'en': 'Revoke for {run} on {host}',
        'de': 'Für {run} auf {host} zurücknehmen'},
    "The HOME of {name} changed since its last start ({since}) in files that decide what runs at a start (shell, git, ssh, the agent's settings, hooks, plugins, skills):": {
        'en': "The HOME of {name} changed since its last start ({since}) in files that decide what runs at a start (shell, git, ssh, the agent's settings, hooks, plugins, skills):",
        'de': 'Das HOME von {name} hat sich seit dem letzten Start ({since}) in Dateien geändert, die bestimmen, was bei einem Start läuft (Shell, git, ssh, die Einstellungen des Agenten, Hooks, Plugins, Skills):'},
    'The limits it has now could not be read: {error}': {
        'en': 'The limits it has now could not be read: {error}',
        'de': 'Ihre aktuellen Grenzen ließen sich nicht lesen: {error}'},
    "The page opens in this computer's browser, not on {host}.": {
        'en': "The page opens in this computer's browser, not on {host}.",
        'de': 'Die Seite öffnet sich im Browser dieses Rechners, nicht auf {host}.'},
    'This machine': {
        'en': 'This machine',
        'de': 'Dieser Rechner'},
    'What changed in the HOME of {name} since its last start could not be checked:\n{error}\n\nResume it anyway?': {
        'en': 'What changed in the HOME of {name} since its last start could not be checked:\n{error}\n\nResume it anyway?',
        'de': 'Was sich seit dem letzten Start im HOME von {name} geändert hat, ließ sich nicht prüfen:\n{error}\n\nTrotzdem fortsetzen?'},
    "Whose sandboxes are listed: this machine's, a host's, or all of them": {
        'en': "Whose sandboxes are listed: this machine's, a host's, or all of them",
        'de': 'Wessen Sandboxen gelistet werden: die dieses Rechners, die eines Hosts oder alle'},
    'added': {
        'en': 'added',
        'de': 'hinzugefügt'},
    'changed': {
        'en': 'changed',
        'de': 'geändert'},
    'permissions': {
        'en': 'permissions',
        'de': 'Rechte'},
    'removed': {
        'en': 'removed',
        'de': 'entfernt'},
    'sandbox-ai did not remove {path} from {run}:\n\n{reason}\n\nDetach it anyway? Whatever is open there stays usable until it is closed; a restart takes it away for sure.': {
        'en': 'sandbox-ai did not remove {path} from {run}:\n\n{reason}\n\nDetach it anyway? Whatever is open there stays usable until it is closed; a restart takes it away for sure.',
        'de': 'sandbox-ai hat {path} nicht von {run} entfernt:\n\n{reason}\n\nTrotzdem lösen? Was dort offen ist, bleibt nutzbar, bis es geschlossen wird; sicher nimmt ihn nur ein Neustart weg.'},
    'type': {
        'en': 'type',
        'de': 'Art'},
    '{action} on {host}': {
        'en': '{action} on {host}',
        'de': '{action} auf {host}'},
    '{count} open questions, on this machine and every host': {
        'en': '{count} open questions, on this machine and every host',
        'de': '{count} offene Fragen, auf diesem Rechner und allen Hosts'},
    '{host} asks to open: {url}': {
        'en': '{host} asks to open: {url}',
        'de': '{host} möchte öffnen: {url}'},
    '{run} on {host}': {
        'en': '{run} on {host}',
        'de': '{run} auf {host}'},
    '{sandbox} on {host}': {
        'en': '{sandbox} on {host}',
        'de': '{sandbox} auf {host}'},
    '… and {count} more': {
        'en': '… and {count} more',
        'de': '… und {count} weitere'},
    # round 2: per-question answers, the fleet view, host management, LLM servers
    'not asked yet': {
        'en': 'not asked yet',
        'de': 'noch nicht gefragt'},
    'no answer (last seen {seconds} s ago)': {
        'en': 'no answer (last seen {seconds} s ago)',
        'de': 'keine Antwort (zuletzt gesehen vor {seconds} s)'},
    'no answer': {
        'en': 'no answer',
        'de': 'keine Antwort'},
    'the security mode from {old} down to {new}': {
        'en': 'the security mode from {old} down to {new}',
        'de': 'den Sicherheitsmodus von {old} herab auf {new}'},
    'no canaries of {categories} any more': {
        'en': 'no canaries of {categories} any more',
        'de': 'keine Köder mehr für {categories}'},
    "the agents' telemetry from {old} to {new}": {
        'en': "the agents' telemetry from {old} to {new}",
        'de': 'die Telemetrie der Agenten von {old} auf {new}'},
    'kept starts unused for {days} days removed with their HOME': {
        'en': 'kept starts unused for {days} days removed with their HOME',
        'de': 'aufbewahrte Starts, {days} Tage unbenutzt, samt ihrem HOME entfernt'},
    'Edit {host}': {
        'en': 'Edit {host}',
        'de': '{host} bearbeiten'},
    'Its sandbox-ai is a system installation: update it with sudo': {
        'en': 'Its sandbox-ai is a system installation: update it with sudo',
        'de': 'Sein sandbox-ai ist eine Systeminstallation: mit sudo aktualisieren'},
    'Show starts': {
        'en': 'Show starts',
        'de': 'Starts zeigen'},
    "The Sandboxes page with this host's starts only": {
        'en': "The Sandboxes page with this host's starts only",
        'de': 'Die Seite Sandboxen, nur mit den Starts dieses Hosts'},
    'Show questions': {
        'en': 'Show questions',
        'de': 'Fragen zeigen'},
    "The Approvals page with this host's questions only": {
        'en': "The Approvals page with this host's questions only",
        'de': 'Die Seite Freigaben, nur mit den Fragen dieses Hosts'},
    'Look at every host now (their starts and questions)': {
        'en': 'Look at every host now (their starts and questions)',
        'de': 'Jetzt bei jedem Host nachsehen (seine Starts und Fragen)'},
    'Refresh the hosts': {
        'en': 'Refresh the hosts',
        'de': 'Hosts neu lesen'},
    'Add a host reached over SSH (sandbox-ai hosts add)': {
        'en': 'Add a host reached over SSH (sandbox-ai hosts add)',
        'de': 'Einen über SSH erreichbaren Host hinzufügen (sandbox-ai hosts add)'},
    "Change the host's settings (sandbox-ai hosts set)": {
        'en': "Change the host's settings (sandbox-ai hosts set)",
        'de': 'Die Einstellungen des Hosts ändern (sandbox-ai hosts set)'},
    'Edit the host': {
        'en': 'Edit the host',
        'de': 'Host bearbeiten'},
    'Forget the host (sandbox-ai hosts remove); asked first': {
        'en': 'Forget the host (sandbox-ai hosts remove); asked first',
        'de': 'Den Host vergessen (sandbox-ai hosts remove); vorher gefragt'},
    'Remove the host': {
        'en': 'Remove the host',
        'de': 'Host entfernen'},
    "SSH, the host's sandbox-ai and its doctor (sandbox-ai hosts test)": {
        'en': "SSH, the host's sandbox-ai and its doctor (sandbox-ai hosts test)",
        'de': 'SSH, das sandbox-ai des Hosts und sein Doctor (sandbox-ai hosts test)'},
    'Test the host': {
        'en': 'Test the host',
        'de': 'Host testen'},
    'Update the host': {
        'en': 'Update the host',
        'de': 'Host aktualisieren'},
    'Ask every host for its sandbox-ai version now': {
        'en': 'Ask every host for its sandbox-ai version now',
        'de': 'Jeden Host jetzt nach seiner sandbox-ai-Version fragen'},
    'Security mode': {
        'en': 'Security mode',
        'de': 'Sicherheitsmodus'},
    'Telemetry': {
        'en': 'Telemetry',
        'de': 'Telemetrie'},
    'Canary categories': {
        'en': 'Canary categories',
        'de': 'Köder-Kategorien'},
    'Remove kept starts unused for': {
        'en': 'Remove kept starts unused for',
        'de': 'Aufbewahrte Starts entfernen, unbenutzt seit'},
    'Remove kept starts after a while': {
        'en': 'Remove kept starts after a while',
        'de': 'Aufbewahrte Starts nach einer Weile entfernen'},
    'Days': {
        'en': 'Days',
        'de': 'Tage'},
    'Kept starts': {
        'en': 'Kept starts',
        'de': 'Aufbewahrte Starts'},
    'Save defaults…': {
        'en': 'Save defaults…',
        'de': 'Vorgaben speichern…'},
    'Write the defaults of every later start on the host (sandbox-ai defaults set/unset there); a step down in security is asked first': {
        'en': 'Write the defaults of every later start on the host (sandbox-ai defaults set/unset there); a step down in security is asked first',
        'de': 'Die Vorgaben jedes späteren Starts auf dem Host schreiben (dort sandbox-ai defaults set/unset); weniger Sicherheit wird vorher gefragt'},
    "sandbox-ai's default ({mode})": {
        'en': "sandbox-ai's default ({mode})",
        'de': 'Vorgabe von sandbox-ai ({mode})'},
    "sandbox-ai's default ({categories})": {
        'en': "sandbox-ai's default ({categories})",
        'de': 'Vorgabe von sandbox-ai ({categories})'},
    'these categories:': {
        'en': 'these categories:',
        'de': 'diese Kategorien:'},
    'To change the address, the port or the key: remove the host and add it again (its host key is learnt anew).': {
        'en': 'To change the address, the port or the key: remove the host and add it again (its host key is learnt anew).',
        'de': 'Um Adresse, Port oder Schlüssel zu ändern: den Host entfernen und neu hinzufügen (sein Host-Schlüssel wird neu gelernt).'},
    'Answers': {
        'en': 'Answers',
        'de': 'Antwortet'},
    'days': {
        'en': 'days',
        'de': 'Tage'},
    'This machine is always in the list.': {
        'en': 'This machine is always in the list.',
        'de': 'Dieser Rechner steht immer in der Liste.'},
    'This machine has no SSH settings.': {
        'en': 'This machine has no SSH settings.',
        'de': 'Dieser Rechner hat keine SSH-Einstellungen.'},
    'Nothing changed.': {
        'en': 'Nothing changed.',
        'de': 'Nichts geändert.'},
    'Defaults of {host}': {
        'en': 'Defaults of {host}',
        'de': 'Vorgaben von {host}'},
    'Every later start on {host} gets less protection: {changes}. Save anyway?': {
        'en': 'Every later start on {host} gets less protection: {changes}. Save anyway?',
        'de': 'Jeder spätere Start auf {host} bekommt weniger Schutz: {changes}. Trotzdem speichern?'},
    'Canary categories: {known}': {
        'en': 'Canary categories: {known}',
        'de': 'Köder-Kategorien: {known}'},
    'Open questions': {
        'en': 'Open questions',
        'de': 'Offene Fragen'},
    'Deny for this run': {
        'en': 'Deny for this run',
        'de': 'Für diesen Lauf ablehnen'},
    'Questions of': {
        'en': 'Questions of',
        'de': 'Fragen von'},
    "Whose questions are listed: every host's, this machine's or one host's": {
        'en': "Whose questions are listed: every host's, this machine's or one host's",
        'de': 'Wessen Fragen gelistet sind: die aller Hosts, dieses Rechners oder eines Hosts'},
    'Scope of an allow': {
        'en': 'Scope of an allow',
        'de': 'Geltung einer Erlaubnis'},
    'How long an allow lasts: once, for this run, or always for the project': {
        'en': 'How long an allow lasts: once, for this run, or always for the project',
        'de': 'Wie lange eine Erlaubnis gilt: einmal, für diesen Lauf oder immer für das Projekt'},
    'Deny once': {
        'en': 'Deny once',
        'de': 'Einmal ablehnen'},
    'Refused this time only: the sandbox may ask again': {
        'en': 'Refused this time only: the sandbox may ask again',
        'de': 'Nur diesmal abgelehnt: die Sandbox darf wieder fragen'},
    'Block always…': {
        'en': 'Block always…',
        'de': 'Immer sperren…'},
    'History of the answers': {
        'en': 'History of the answers',
        'de': 'Verlauf der Antworten'},
    'Allowed or blocked always': {
        'en': 'Allowed or blocked always',
        'de': 'Immer erlaubt oder gesperrt'},
    'Remembered on': {
        'en': 'Remembered on',
        'de': 'Gemerkt auf'},
    "Whose list: this machine's or a host's": {
        'en': "Whose list: this machine's or a host's",
        'de': 'Wessen Liste: die dieses Rechners oder eines Hosts'},
    'Refresh the remembered answers': {
        'en': 'Refresh the remembered answers',
        'de': 'Gemerkte Antworten neu lesen'},
    'Select a question first.': {
        'en': 'Select a question first.',
        'de': 'Wählen Sie zuerst eine Frage.'},
    'Only names and addresses can be blocked always.': {
        'en': 'Only names and addresses can be blocked always.',
        'de': 'Nur Namen und Adressen lassen sich immer sperren.'},
    'Refused now and at every later start of this project, without a question. Forget… on this page lifts it.': {
        'en': 'Refused now and at every later start of this project, without a question. Forget… on this page lifts it.',
        'de': 'Jetzt und bei jedem späteren Start dieses Projekts ohne Frage abgelehnt. Vergessen… auf dieser Seite hebt es auf.'},
    'Block always?': {
        'en': 'Block always?',
        'de': 'Immer sperren?'},
    'Open here and allow': {
        'en': 'Open here and allow',
        'de': 'Hier öffnen und erlauben'},
    'The answer is on its way.': {
        'en': 'The answer is on its way.',
        'de': 'Die Antwort ist unterwegs.'},
    'Expired: sandbox-ai refused the question already.': {
        'en': 'Expired: sandbox-ai refused the question already.',
        'de': 'Abgelaufen: sandbox-ai hat die Frage schon abgelehnt.'},
    'Its time is up: sandbox-ai refused it already.': {
        'en': 'Its time is up: sandbox-ai refused it already.',
        'de': 'Ihre Zeit ist um: sandbox-ai hat sie schon abgelehnt.'},
    "Allowed at most “{scope}” here (the policy, the run's security mode or the kind of question).": {
        'en': "Allowed at most “{scope}” here (the policy, the run's security mode or the kind of question).",
        'de': 'Hier höchstens „{scope}“ erlaubt (die Richtlinie, der Sicherheitsmodus des Laufs oder die Art der Frage).'},
    'End it': {
        'en': 'End it',
        'de': 'Beenden'},
    'the kept start {name} with its HOME (last used {time}, {workspace})': {
        'en': 'the kept start {name} with its HOME (last used {time}, {workspace})',
        'de': 'der aufbewahrte Start {name} samt seinem HOME (zuletzt benutzt {time}, {workspace})'},
    'the leftovers of {name} (ended; its HOME was not kept)': {
        'en': 'the leftovers of {name} (ended; its HOME was not kept)',
        'de': 'die Reste von {name} (beendet; sein HOME wurde nicht aufbewahrt)'},
    'the history of {name}': {
        'en': 'the history of {name}',
        'de': 'der Verlauf von {name}'},
    'left alone: {name}: {reason}': {
        'en': 'left alone: {name}: {reason}',
        'de': 'gelassen: {name}: {reason}'},
    'left alone: {path} is damaged; remove it by hand': {
        'en': 'left alone: {path} is damaged; remove it by hand',
        'de': 'gelassen: {path} ist beschädigt; von Hand entfernen'},
    'Untaint…': {
        'en': 'Untaint…',
        'de': 'Taint aufheben…'},
    'Untaint': {
        'en': 'Untaint',
        'de': 'Taint aufheben'},
    "Take back the run's taint (sandbox-ai net untaint): only once you checked the untrusted content that entered it": {
        'en': "Take back the run's taint (sandbox-ai net untaint): only once you checked the untrusted content that entered it",
        'de': 'Den Taint des Laufs zurücknehmen (sandbox-ai net untaint): erst, wenn Sie die nicht vertrauenswürdigen Inhalte geprüft haben, die hineinkamen'},
    'status {status}': {
        'en': 'status {status}',
        'de': 'Status {status}'},
    'status unknown': {
        'en': 'status unknown',
        'de': 'Status unbekannt'},
    'renamed to {name}': {
        'en': 'renamed to {name}',
        'de': 'umbenannt in {name}'},
    'renamed from {name}': {
        'en': 'renamed from {name}',
        'de': 'umbenannt von {name}'},
    'Ended': {
        'en': 'Ended',
        'de': 'Beendete'},
    'Also the starts that ended: stopped MCP servers and what else sandbox-ai list --all keeps, on this machine and every host': {
        'en': 'Also the starts that ended: stopped MCP servers and what else sandbox-ai list --all keeps, on this machine and every host',
        'de': 'Auch die beendeten Starts: gestoppte MCP-Server und was sandbox-ai list --all sonst aufbewahrt, auf diesem Rechner und jedem Host'},
    'Refresh the sandboxes': {
        'en': 'Refresh the sandboxes',
        'de': 'Sandboxen neu lesen'},
    'Sandboxes and sessions': {
        'en': 'Sandboxes and sessions',
        'de': 'Sandboxen und Sitzungen'},
    '{host} does not answer (last seen {seconds} s ago): its state now is not known.': {
        'en': '{host} does not answer (last seen {seconds} s ago): its state now is not known.',
        'de': '{host} antwortet nicht (zuletzt gesehen vor {seconds} s): sein Zustand jetzt ist unbekannt.'},
    'It ended: History… shows what happened to it.': {
        'en': 'It ended: History… shows what happened to it.',
        'de': 'Er ist beendet: Verlauf… zeigt, was mit ihm geschah.'},
    '{host} does not answer.': {
        'en': '{host} does not answer.',
        'de': '{host} antwortet nicht.'},
    'Rename {name} on {host}': {
        'en': 'Rename {name} on {host}',
        'de': '{name} auf {host} umbenennen'},
    'Rename {name}': {
        'en': 'Rename {name}',
        'de': '{name} umbenennen'},
    'New name (1-40 letters, digits, _ . -; its run id stays)': {
        'en': 'New name (1-40 letters, digits, _ . -; its run id stays)',
        'de': 'Neuer Name (1-40 Buchstaben, Ziffern, _ . -; seine Lauf-ID bleibt)'},
    'History of {name} on {host}': {
        'en': 'History of {name} on {host}',
        'de': 'Verlauf von {name} auf {host}'},
    'History of {name}': {
        'en': 'History of {name}',
        'de': 'Verlauf von {name}'},
    'Clean up on {host}': {
        'en': 'Clean up on {host}',
        'de': 'Aufräumen auf {host}'},
    'Clean up': {
        'en': 'Clean up',
        'de': 'Aufräumen'},
    'Untaint {run} on {host}': {
        'en': 'Untaint {run} on {host}',
        'de': 'Taint von {run} auf {host} aufheben'},
    'Untaint {run}': {
        'en': 'Untaint {run}',
        'de': 'Taint von {run} aufheben'},
    'memory': {
        'en': 'memory',
        'de': 'Speicher'},
    'processes': {
        'en': 'processes',
        'de': 'Prozesse'},
    'Unchanged: {limits} (an empty field keeps its limit; none removes it).': {
        'en': 'Unchanged: {limits} (an empty field keeps its limit; none removes it).',
        'de': 'Unverändert: {limits} (ein leeres Feld behält seine Grenze; none entfernt sie).'},
    'Rename…': {
        'en': 'Rename…',
        'de': 'Umbenennen…'},
    'Another name for the selected start; its run id stays (sandbox-ai rename)': {
        'en': 'Another name for the selected start; its run id stays (sandbox-ai rename)',
        'de': 'Ein anderer Name für den gewählten Start; seine Lauf-ID bleibt (sandbox-ai rename)'},
    'History…': {
        'en': 'History…',
        'de': 'Verlauf…'},
    'What happened to the selected start: starts, ends, pauses, renames (sandbox-ai list --history)': {
        'en': 'What happened to the selected start: starts, ends, pauses, renames (sandbox-ai list --history)',
        'de': 'Was mit dem gewählten Start geschah: Starts, Enden, Pausen, Umbenennungen (sandbox-ai list --history)'},
    'Clean up…': {
        'en': 'Clean up…',
        'de': 'Aufräumen…'},
    'Remove leftovers and the kept starts unused for longer than its keep_days, on this machine or the host chosen above (sandbox-ai gc; shown first)': {
        'en': 'Remove leftovers and the kept starts unused for longer than its keep_days, on this machine or the host chosen above (sandbox-ai gc; shown first)',
        'de': 'Reste entfernen und die aufbewahrten Starts, die länger als keep_days unbenutzt sind, auf diesem Rechner oder dem oben gewählten Host (sandbox-ai gc; vorher gezeigt)'},
    'the list of hosts could not be read: {error}': {
        'en': 'the list of hosts could not be read: {error}',
        'de': 'die Liste der Hosts war nicht lesbar: {error}'},
    'Only the starts of agents have a name to change.': {
        'en': 'Only the starts of agents have a name to change.',
        'de': 'Nur Starts von Agenten haben einen änderbaren Namen.'},
    "A name is 1-40 letters, digits, '_', '.' or '-', beginning with a letter or digit.": {
        'en': "A name is 1-40 letters, digits, '_', '.' or '-', beginning with a letter or digit.",
        'de': "Ein Name besteht aus 1-40 Buchstaben, Ziffern, '_', '.' oder '-' und beginnt mit einem Buchstaben oder einer Ziffer."},
    'No history of {name}.': {
        'en': 'No history of {name}.',
        'de': 'Kein Verlauf von {name}.'},
    'Nothing to remove.': {
        'en': 'Nothing to remove.',
        'de': 'Nichts zu entfernen.'},
    'Remove these? Kept starts go with their HOME; this cannot be undone.': {
        'en': 'Remove these? Kept starts go with their HOME; this cannot be undone.',
        'de': 'Diese entfernen? Aufbewahrte Starts gehen samt ihrem HOME; das lässt sich nicht rückgängig machen.'},
    'unchanged (none: no limit)': {
        'en': 'unchanged (none: no limit)',
        'de': 'unverändert (none: keine Grenze)'},
    '{state}, last seen {seconds} s ago': {
        'en': '{state}, last seen {seconds} s ago',
        'de': '{state}, zuletzt gesehen vor {seconds} s'},
    '{state}, not seen': {
        'en': '{state}, not seen',
        'de': '{state}, nicht gesehen'},
    '{state}, asking': {
        'en': '{state}, asking',
        'de': '{state}, fragt'},
    'It waits for an answer on the Approvals page.': {
        'en': 'It waits for an answer on the Approvals page.',
        'de': 'Er wartet auf eine Antwort auf der Seite Freigaben.'},
    '{host} does not answer: {problem}': {
        'en': '{host} does not answer: {problem}',
        'de': '{host} antwortet nicht: {problem}'},
    "An MCP server's sandbox: it only stops (sandbox-ai stop there).": {
        'en': "An MCP server's sandbox: it only stops (sandbox-ai stop there).",
        'de': 'Die Sandbox eines MCP-Servers: sie lässt sich nur stoppen (dort sandbox-ai stop).'},
    'Nothing changed: an empty field leaves its limit as it is. To remove a limit, type none.': {
        'en': 'Nothing changed: an empty field leaves its limit as it is. To remove a limit, type none.',
        'de': 'Nichts geändert: ein leeres Feld lässt seine Grenze, wie sie ist. Um eine Grenze zu entfernen, geben Sie none ein.'},
    'Take back the taint of {run}? Untrusted content entered it; the rules for tainted runs (what it may reach, what is asked) no longer hold until such content enters it again. Only when you checked what came in.': {
        'en': 'Take back the taint of {run}? Untrusted content entered it; the rules for tainted runs (what it may reach, what is asked) no longer hold until such content enters it again. Only when you checked what came in.',
        'de': 'Den Taint von {run} zurücknehmen? Nicht vertrauenswürdige Inhalte kamen hinein; die Regeln für Läufe mit Taint (was er erreichen darf, was gefragt wird) gelten dann nicht mehr, bis wieder solche Inhalte hineinkommen. Nur, wenn Sie geprüft haben, was hereinkam.'},
    'Removed:': {
        'en': 'Removed:',
        'de': 'Entfernt:'},
    'LM Studio': {
        'en': 'LM Studio',
        'de': 'LM Studio'},
    'Ollama': {
        'en': 'Ollama',
        'de': 'Ollama'},
    'llama.cpp server': {
        'en': 'llama.cpp server',
        'de': 'llama.cpp-Server'},
    'vLLM': {
        'en': 'vLLM',
        'de': 'vLLM'},
    'OpenAI-compatible server (a gateway, LiteLLM, …)': {
        'en': 'OpenAI-compatible server (a gateway, LiteLLM, …)',
        'de': 'OpenAI-kompatibler Server (ein Gateway, LiteLLM, …)'},
    'The server lists no models.': {
        'en': 'The server lists no models.',
        'de': 'Der Server nennt keine Modelle.'},
    'Loaded': {
        'en': 'Loaded',
        'de': 'Geladen'},
    'New LLM server on {host}': {
        'en': 'New LLM server on {host}',
        'de': 'Neuer LLM-Server auf {host}'},
    "The server's root, http(s)://HOST[:PORT][/PREFIX]; each agent gets the API path it needs. Plain http only to this host, private or Tailscale addresses.": {
        'en': "The server's root, http(s)://HOST[:PORT][/PREFIX]; each agent gets the API path it needs. Plain http only to this host, private or Tailscale addresses.",
        'de': 'Die Wurzel des Servers, http(s)://HOST[:PORT][/PRÄFIX]; jeder Agent bekommt den API-Pfad, den er braucht. Unverschlüsseltes http nur zu diesem Rechner, privaten oder Tailscale-Adressen.'},
    'The model a start uses when --llm names none (optional)': {
        'en': 'The model a start uses when --llm names none (optional)',
        'de': 'Das Modell, das ein Start nimmt, wenn --llm keines nennt (optional)'},
    'The VPN profile the server is reached through (optional)': {
        'en': 'The VPN profile the server is reached through (optional)',
        'de': 'Das VPN-Profil, über das der Server erreicht wird (optional)'},
    'It also offers the Anthropic Messages API (Claude Code)': {
        'en': 'It also offers the Anthropic Messages API (Claude Code)',
        'de': 'Er bietet auch die Anthropic Messages API (Claude Code)'},
    'URL': {
        'en': 'URL',
        'de': 'URL'},
    'Secret for the API key (optional)': {
        'en': 'Secret for the API key (optional)',
        'de': 'Geheimnis für den API-Schlüssel (optional)'},
    "A key typed here is kept as a secret of sandbox-ai (standard input, never a command line); the server's entry only names that secret, and the proxy adds the key on the wire.": {
        'en': "A key typed here is kept as a secret of sandbox-ai (standard input, never a command line); the server's entry only names that secret, and the proxy adds the key on the wire.",
        'de': 'Ein hier eingegebener Schlüssel wird als Geheimnis von sandbox-ai abgelegt (Standardeingabe, nie eine Befehlszeile); der Eintrag des Servers nennt nur dieses Geheimnis, und der Proxy fügt den Schlüssel unterwegs hinzu.'},
    "A server's name is 1-32 letters, digits, '_' or '-', not starting with '-'.": {
        'en': "A server's name is 1-32 letters, digits, '_' or '-', not starting with '-'.",
        'de': "Der Name eines Servers sind 1-32 Buchstaben, Ziffern, '_' oder '-', nicht mit '-' am Anfang."},
    'Choose the kind of server.': {
        'en': 'Choose the kind of server.',
        'de': 'Die Art des Servers wählen.'},
    'The URL is http(s)://HOST[:PORT][/PREFIX], without user, query or fragment.': {
        'en': 'The URL is http(s)://HOST[:PORT][/PREFIX], without user, query or fragment.',
        'de': 'Die URL ist http(s)://HOST[:PORT][/PRÄFIX], ohne Benutzer, Abfrage oder Fragment.'},
    'LLM servers': {
        'en': 'LLM servers',
        'de': 'LLM-Server'},
    'every agent': {
        'en': 'every agent',
        'de': 'jeder Agent'},
    "Check the API this agent needs (or every agent's)": {
        'en': "Check the API this agent needs (or every agent's)",
        'de': 'Die API prüfen, die dieser Agent braucht (oder die jedes Agenten)'},
    'Models': {
        'en': 'Models',
        'de': 'Modelle'},
    'Adding {name} on {host}…': {
        'en': 'Adding {name} on {host}…',
        'de': '{name} wird auf {host} hinzugefügt…'},
    'the server works for it': {
        'en': 'the server works for it',
        'de': 'der Server taugt dafür'},
    'the check found problems': {
        'en': 'the check found problems',
        'de': 'die Prüfung fand Probleme'},
    'Check of {name} on {host}': {
        'en': 'Check of {name} on {host}',
        'de': 'Prüfung von {name} auf {host}'},
    'Models listed: {count}': {
        'en': 'Models listed: {count}',
        'de': 'Genannte Modelle: {count}'},
    'Models of {server}': {
        'en': 'Models of {server}',
        'de': 'Modelle von {server}'},
    'Not a model name: {model}': {
        'en': 'Not a model name: {model}',
        'de': 'Kein Modellname: {model}'},
    "Not a VPN profile's name: {name}": {
        'en': "Not a VPN profile's name: {name}",
        'de': 'Kein Name eines VPN-Profils: {name}'},
    "A secret's name is letters, digits and '_', not starting with a digit.": {
        'en': "A secret's name is letters, digits and '_', not starting with a digit.",
        'de': "Der Name eines Geheimnisses sind Buchstaben, Ziffern und '_', nicht mit einer Ziffer am Anfang."},
    'LLM servers (sandbox-ai llm) the agents can use: start one with --llm NAME. Keys stay secrets of sandbox-ai; the connection proxy adds them on the wire, so an agent only sees a placeholder.': {
        'en': 'LLM servers (sandbox-ai llm) the agents can use: start one with --llm NAME. Keys stay secrets of sandbox-ai; the connection proxy adds them on the wire, so an agent only sees a placeholder.',
        'de': 'LLM-Server (sandbox-ai llm), die die Agenten nutzen können: ein Start mit --llm NAME nimmt einen davon. Schlüssel bleiben Geheimnisse von sandbox-ai; der Verbindungs-Proxy fügt sie unterwegs hinzu, ein Agent sieht nur einen Platzhalter.'},
    'Add a server': {
        'en': 'Add a server',
        'de': 'Einen Server hinzufügen'},
    'Add an LLM server on the chosen host': {
        'en': 'Add an LLM server on the chosen host',
        'de': 'Einen LLM-Server auf dem gewählten Host hinzufügen'},
    "Reachability, TLS, the key, the models and the agents' APIs": {
        'en': "Reachability, TLS, the key, the models and the agents' APIs",
        'de': 'Erreichbarkeit, TLS, der Schlüssel, die Modelle und die APIs der Agenten'},
    'The models the server offers now': {
        'en': 'The models the server offers now',
        'de': 'Die Modelle, die der Server jetzt anbietet'},
    'Forget the server (its secrets stay in sandbox-ai secrets)': {
        'en': 'Forget the server (its secrets stay in sandbox-ai secrets)',
        'de': 'Den Server vergessen (seine Geheimnisse bleiben in sandbox-ai secrets)'},
    'Ask the chosen host again': {
        'en': 'Ask the chosen host again',
        'de': 'Den gewählten Host erneut fragen'},
    'LLM servers on {host}: {count}': {
        'en': 'LLM servers on {host}: {count}',
        'de': 'LLM-Server auf {host}: {count}'},
    'No LLM server yet on {host}.': {
        'en': 'No LLM server yet on {host}.',
        'de': 'Noch kein LLM-Server auf {host}.'},
    "Refused: {name} is not a server's name that can be passed on.": {
        'en': "Refused: {name} is not a server's name that can be passed on.",
        'de': 'Abgelehnt: {name} ist kein Servername, der weitergegeben werden kann.'},
    'Could not add {name} on {host}.': {
        'en': 'Could not add {name} on {host}.',
        'de': '{name} konnte auf {host} nicht hinzugefügt werden.'},
    'New LLM server': {
        'en': 'New LLM server',
        'de': 'Neuer LLM-Server'},
    'Forget the LLM server {name} on {host}? Starts with {option} cannot start until it is added again.': {
        'en': 'Forget the LLM server {name} on {host}? Starts with {option} cannot start until it is added again.',
        'de': 'Den LLM-Server {name} auf {host} vergessen? Starts mit {option} können nicht starten, bis er wieder hinzugefügt ist.'},
    'Could not forget {name} on {host}: {error}': {
        'en': 'Could not forget {name} on {host}: {error}',
        'de': '{name} konnte auf {host} nicht vergessen werden: {error}'},
    '{name} removed.': {
        'en': '{name} removed.',
        'de': '{name} entfernt.'},
    'Could not list the LLM servers on {host}: {error}': {
        'en': 'Could not list the LLM servers on {host}: {error}',
        'de': 'Die LLM-Server auf {host} konnten nicht aufgelistet werden: {error}'},
    'A typed key needs the name of the secret to keep it as.': {
        'en': 'A typed key needs the name of the secret to keep it as.',
        'de': 'Ein eingegebener Schlüssel braucht den Namen des Geheimnisses, unter dem er abgelegt wird.'},
    '{name} added.': {
        'en': '{name} added.',
        'de': '{name} hinzugefügt.'},
    'Could not list the models of {name}: {error}': {
        'en': 'Could not list the models of {name}: {error}',
        'de': 'Die Modelle von {name} konnten nicht aufgelistet werden: {error}'},
    'Model servers of your own, kept by name on this machine or a host: Ollama, LM Studio, llama.cpp, vLLM or another OpenAI-compatible one, local or behind a VPN. A start with --llm NAME reaches only that server; its key stays with the proxy.': {
        'en': 'Model servers of your own, kept by name on this machine or a host: Ollama, LM Studio, llama.cpp, vLLM or another OpenAI-compatible one, local or behind a VPN. A start with --llm NAME reaches only that server; its key stays with the proxy.',
        'de': 'Eigene Modellserver, unter einem Namen aufbewahrt auf diesem Rechner oder einem Host: Ollama, LM Studio, llama.cpp, vLLM oder ein anderer OpenAI-kompatibler, lokal oder hinter einem VPN. Ein Start mit --llm NAME erreicht nur diesen Server; sein Schlüssel bleibt beim Proxy.'},
    'Add the server you run, then Check it for the agent you use.': {
        'en': 'Add the server you run, then Check it for the agent you use.',
        'de': 'Fügen Sie den Server hinzu, den Sie betreiben, und prüfen Sie ihn dann mit Prüfen für den Agenten, den Sie nutzen.'},
    'On {host} this can only be denied: {reason}': {
        'en': 'On {host} this can only be denied: {reason}',
        'de': 'Auf {host} lässt sich das nur ablehnen: {reason}'},
    '{host} does not let the page be opened elsewhere: {reason}': {
        'en': '{host} does not let the page be opened elsewhere: {reason}',
        'de': '{host} lässt die Seite nicht anderswo öffnen: {reason}'},
    'The address came shortened from {host}: it is not opened here.': {
        'en': 'The address came shortened from {host}: it is not opened here.',
        'de': 'Die Adresse kam gekürzt von {host}: sie wird hier nicht geöffnet.'},
    '{host} asks to open a page on {destination}': {
        'en': '{host} asks to open a page on {destination}',
        'de': '{host} möchte eine Seite auf {destination} öffnen'},
    '(shortened)': {
        'en': '(shortened)',
        'de': '(gekürzt)'},
    'A running {start} is stopped and started again for it: what runs in it now ends.': {
        'en': 'A running {start} is stopped and started again for it: what runs in it now ends.',
        'de': 'Ein laufender Start {start} wird dafür gestoppt und neu gestartet: Was jetzt darin läuft, endet.'},
    'A secret {name} is kept already: replace its value? Everything that names it gets the new one.': {
        'en': 'A secret {name} is kept already: replace its value? Everything that names it gets the new one.',
        'de': 'Ein Geheimnis {name} ist schon abgelegt: seinen Wert ersetzen? Alles, was es nennt, bekommt den neuen.'},
    'A secret {name} is kept already: the key typed replaces its value, for everything that names it. Replace it?': {
        'en': 'A secret {name} is kept already: the key typed replaces its value, for everything that names it. Replace it?',
        'de': 'Ein Geheimnis {name} ist schon abgelegt: Der eingegebene Schlüssel ersetzt seinen Wert, für alles, was es nennt. Ersetzen?'},
    'Add {what} to the kit {name}? Every later start with this kit gets it; a plugin or a hook runs code there.': {
        'en': 'Add {what} to the kit {name}? Every later start with this kit gets it; a plugin or a hook runs code there.',
        'de': '{what} zum Kit {name} hinzufügen? Jeder spätere Start mit diesem Kit bekommt es; ein Plugin oder ein Hook führt dort Code aus.'},
    'Allow with the scope chosen (Ctrl+Return)': {
        'en': 'Allow with the scope chosen (Ctrl+Return)',
        'de': 'Mit der gewählten Geltung erlauben (Strg+Eingabe)'},
    'Allowed this time only (Ctrl+Return)': {
        'en': 'Allowed this time only (Ctrl+Return)',
        'de': 'Nur dieses Mal erlaubt (Strg+Eingabe)'},
    'Answered: {host} lists it until it has read the answer.': {
        'en': 'Answered: {host} lists it until it has read the answer.',
        'de': 'Beantwortet: {host} listet sie, bis er die Antwort gelesen hat.'},
    'Change': {
        'en': 'Change',
        'de': 'Ändern'},
    'Give {start} the tools of the MCP server {name}?': {
        'en': 'Give {start} the tools of the MCP server {name}?',
        'de': '{start} die Werkzeuge des MCP-Servers {name} geben?'},
    'History of': {
        'en': 'History of',
        'de': 'Verlauf von'},
    'History of the answers on the host chosen': {
        'en': 'History of the answers on the host chosen',
        'de': 'Verlauf der Antworten auf dem gewählten Host'},
    'Let {run} ({where}) reach this until it ends, at once?\n\n{what}': {
        'en': 'Let {run} ({where}) reach this until it ends, at once?\n\n{what}',
        'de': '{run} ({where}) ab sofort bis zu seinem Ende dies erreichen lassen?\n\n{what}'},
    'Let {run} use more of its host until it ends: {changes}?': {
        'en': 'Let {run} use more of its host until it ends: {changes}?',
        'de': '{run} bis zu seinem Ende mehr von seinem Host nutzen lassen: {changes}?'},
    'Looking for the secret {name}…': {
        'en': 'Looking for the secret {name}…',
        'de': 'Suche das Geheimnis {name}…'},
    'No answers on {host} yet.': {
        'en': 'No answers on {host} yet.',
        'de': 'Noch keine Antworten auf {host}.'},
    'Not added: the secret {name} stays as it is.': {
        'en': 'Not added: the secret {name} stays as it is.',
        'de': 'Nicht hinzugefügt: Das Geheimnis {name} bleibt, wie es ist.'},
    'Not kept: the secret {name} stays as it is.': {
        'en': 'Not kept: the secret {name} stays as it is.',
        'de': 'Nicht abgelegt: Das Geheimnis {name} bleibt, wie es ist.'},
    'Not known how much was saved on {host}: {error}. The values read again show what it has now.': {
        'en': 'Not known how much was saved on {host}: {error}. The values read again show what it has now.',
        'de': 'Nicht bekannt, wie viel auf {host} gespeichert wurde: {error}. Die neu gelesenen Werte zeigen, was er jetzt hat.'},
    'Nothing was removed: what would go has changed since it was shown. Clean up… shows it again.': {
        'en': 'Nothing was removed: what would go has changed since it was shown. Clean up… shows it again.',
        'de': 'Nichts wurde entfernt: Was entfernt würde, hat sich geändert, seit es gezeigt wurde. Aufräumen… zeigt es erneut.'},
    'On another host the key is a secret of sandbox-ai there: keep it on that host first (Credentials › Secrets, with that host chosen), then name it here.': {
        'en': 'On another host the key is a secret of sandbox-ai there: keep it on that host first (Credentials › Secrets, with that host chosen), then name it here.',
        'de': 'Auf einem anderen Host ist der Schlüssel ein Geheimnis von sandbox-ai dort: Legen Sie es zuerst auf diesem Host ab (Zugangsdaten › Geheimnisse, mit diesem Host gewählt), und nennen Sie es dann hier.'},
    'Raise': {
        'en': 'Raise',
        'de': 'Erhöhen'},
    'Refresh the history': {
        'en': 'Refresh the history',
        'de': 'Den Verlauf aktualisieren'},
    'Refused now and at every later start of this project, without a question (Ctrl+Shift+K, asked first)': {
        'en': 'Refused now and at every later start of this project, without a question (Ctrl+Shift+K, asked first)',
        'de': 'Jetzt und bei jedem späteren Start dieses Projekts abgelehnt, ohne Frage (Strg+Umschalt+K, mit Rückfrage)'},
    'Refused now and at every later start of this project, without a question (Forget… below lifts it; Ctrl+Shift+K)': {
        'en': 'Refused now and at every later start of this project, without a question (Forget… below lifts it; Ctrl+Shift+K)',
        'de': 'Jetzt und bei jedem späteren Start dieses Projekts abgelehnt, ohne Frage (Vergessen… unten hebt es auf; Strg+Umschalt+K)'},
    'Refused; not asked again in this run (Ctrl+Shift+D)': {
        'en': 'Refused; not asked again in this run (Ctrl+Shift+D)',
        'de': 'Abgelehnt; in diesem Lauf nicht mehr gefragt (Strg+Umschalt+D)'},
    'Replace': {
        'en': 'Replace',
        'de': 'Ersetzen'},
    'Saved on {host}: {applied}. Refused: {refused} ({reason}). Not tried: {rest}.': {
        'en': 'Saved on {host}: {applied}. Refused: {refused} ({reason}). Not tried: {rest}.',
        'de': 'Gespeichert auf {host}: {applied}. Abgelehnt: {refused} ({reason}). Nicht versucht: {rest}.'},
    'Secrets on': {
        'en': 'Secrets on',
        'de': 'Geheimnisse auf'},
    'Take {what} ({agent}) out of the kit {name}? Every later start with this kit is without it.': {
        'en': 'Take {what} ({agent}) out of the kit {name}? Every later start with this kit is without it.',
        'de': '{what} ({agent}) aus dem Kit {name} nehmen? Jeder spätere Start mit diesem Kit ist ohne es.'},
    'The limits it has now are not known: this may be more than now.': {
        'en': 'The limits it has now are not known: this may be more than now.',
        'de': 'Die Grenzen, die er jetzt hat, sind nicht bekannt: Das kann mehr sein als jetzt.'},
    'They wait on the Approvals page.': {
        'en': 'They wait on the Approvals page.',
        'de': 'Sie warten auf der Seite Freigaben.'},
    "This machine's secrets, or a host's (over SSH)": {
        'en': "This machine's secrets, or a host's (over SSH)",
        'de': 'Die Geheimnisse dieses Rechners oder eines Hosts (über SSH)'},
    'This run approves at most once, and an allowed name stays pinned for the run: deny it, or allow the name at the next start (--allow-host).': {
        'en': 'This run approves at most once, and an allowed name stays pinned for the run: deny it, or allow the name at the next start (--allow-host).',
        'de': 'Dieser Lauf erlaubt höchstens einmal, und ein erlaubter Name bleibt für den Lauf festgehalten: Lehnen Sie ab, oder erlauben Sie den Namen beim nächsten Start (--allow-host).'},
    'Update sandbox-ai on {host} with sudo from now on?': {
        'en': 'Update sandbox-ai on {host} with sudo from now on?',
        'de': 'sandbox-ai auf {host} ab jetzt mit sudo aktualisieren?'},
    'Update sandbox-ai on {host} without sudo from now on?': {
        'en': 'Update sandbox-ai on {host} without sudo from now on?',
        'de': 'sandbox-ai auf {host} ab jetzt ohne sudo aktualisieren?'},
    'What was added there could not be read: {error}': {
        'en': 'What was added there could not be read: {error}',
        'de': 'Was dort hinzugefügt wurde, ließ sich nicht lesen: {error}'},
    'Whether a secret {name} is kept already could not be told: the key typed would replace its value. Go on?': {
        'en': 'Whether a secret {name} is kept already could not be told: the key typed would replace its value. Go on?',
        'de': 'Ob ein Geheimnis {name} schon abgelegt ist, ließ sich nicht feststellen: Der eingegebene Schlüssel würde seinen Wert ersetzen. Weiter?'},
    'Whether a secret {name} is kept there could not be told: a value it has would be replaced. Go on?': {
        'en': 'Whether a secret {name} is kept there could not be told: a value it has would be replaced. Go on?',
        'de': 'Ob dort ein Geheimnis {name} abgelegt ist, ließ sich nicht feststellen: Ein Wert, den es hat, würde ersetzt. Weiter?'},
    "Whose answers: this machine's or a host's": {
        'en': "Whose answers: this machine's or a host's",
        'de': 'Wessen Antworten: die dieses Rechners oder die eines Hosts'},
    'Without an answer within {seconds} seconds a question is refused; a page to open waits until it is answered. A deny lasts at most for the run; “always” is remembered for the project. Allowing a URL opens it in your browser.': {
        'en': 'Without an answer within {seconds} seconds a question is refused; a page to open waits until it is answered. A deny lasts at most for the run; “always” is remembered for the project. Allowing a URL opens it in your browser.',
        'de': 'Ohne Antwort innerhalb von {seconds} Sekunden wird eine Frage abgelehnt; eine zu öffnende Seite wartet, bis sie beantwortet ist. Ein Ablehnen gilt höchstens für den Lauf; „immer“ wird für das Projekt gemerkt. Eine erlaubte URL öffnet sich in Ihrem Browser.'},
    'answered': {
        'en': 'answered',
        'de': 'beantwortet'},
    'kept starts unused for {days} days removed with their HOME (instead of {before})': {
        'en': 'kept starts unused for {days} days removed with their HOME (instead of {before})',
        'de': 'aufbewahrte Starts, {days} Tage ungenutzt, samt HOME entfernt (statt nach {before})'},
    'on this machine': {
        'en': 'on this machine',
        'de': 'auf diesem Rechner'},
    'on {host}': {
        'en': 'on {host}',
        'de': 'auf {host}'},
    'sandbox-ai asks: {count} more questions': {
        'en': 'sandbox-ai asks: {count} more questions',
        'de': 'sandbox-ai fragt: {count} weitere Fragen'},
    "{setting}: sandbox-ai's default": {
        'en': "{setting}: sandbox-ai's default",
        'de': '{setting}: Vorgabe von sandbox-ai'},
    '{limit}: no limit': {
        'en': '{limit}: no limit',
        'de': '{limit}: keine Grenze'},
    '{limit}: {value}': {
        'en': '{limit}: {value}',
        'de': '{limit}: {value}'},
    '{name} added, but its key was not kept as the secret {secret}: {error}': {
        'en': '{name} added, but its key was not kept as the secret {secret}: {error}',
        'de': '{name} hinzugefügt, aber sein Schlüssel wurde nicht als Geheimnis {secret} abgelegt: {error}'},
    '  {host}: the installation runs in a terminal tab, where sudo asks for the password': {
        'en': '  {host}: the installation runs in a terminal tab, where sudo asks for the password',
        'de': '  {host}: Die Installation läuft in einem Terminal-Tab, wo sudo nach dem Passwort fragt'},
    'A click takes the folder; a double-click or Enter takes it and goes on': {
        'en': 'A click takes the folder; a double-click or Enter takes it and goes on',
        'de': 'Ein Klick übernimmt den Ordner; ein Doppelklick oder die Eingabetaste übernimmt ihn und geht weiter'},
    'As whom': {
        'en': 'As whom',
        'de': 'Als wer'},
    'Command (double-click the row or Copy copies it): {command}': {
        'en': 'Command (double-click the row or Copy copies it): {command}',
        'de': 'Befehl (ein Doppelklick auf die Zeile oder Kopieren kopiert ihn): {command}'},
    'Folders used last': {
        'en': 'Folders used last',
        'de': 'Zuletzt genutzte Ordner'},
    'For every user, under:': {
        'en': 'For every user, under:',
        'de': 'Für jeden Benutzer, unter:'},
    'For the SSH user ({user}): in its ~/.local': {
        'en': 'For the SSH user ({user}): in its ~/.local',
        'de': 'Für den SSH-Benutzer ({user}): in seinem ~/.local'},
    'From': {
        'en': 'From',
        'de': 'Woher'},
    'How to fix: {hint}': {
        'en': 'How to fix: {hint}',
        'de': 'So beheben Sie es: {hint}'},
    'Install': {
        'en': 'Install',
        'de': 'Installieren'},
    'Install sandbox-ai on the host': {
        'en': 'Install sandbox-ai on the host',
        'de': 'sandbox-ai auf dem Host installieren'},
    "Install sandbox-ai on this host: for the SSH user or every user, as root, with sudo or without, from this machine's copy or the signed release (sandbox-ai hosts install)": {
        'en': "Install sandbox-ai on this host: for the SSH user or every user, as root, with sudo or without, from this machine's copy or the signed release (sandbox-ai hosts install)",
        'de': 'sandbox-ai auf diesem Host installieren: für den SSH-Benutzer oder jeden Benutzer, als root, mit oder ohne sudo, aus der Kopie dieses Rechners oder der signierten Version (sandbox-ai hosts install)'},
    'Install sandbox-ai on {host}': {
        'en': 'Install sandbox-ai on {host}',
        'de': 'sandbox-ai auf {host} installieren'},
    'Install sandbox-ai on {host} {how}? An installation there is replaced.': {
        'en': 'Install sandbox-ai on {host} {how}? An installation there is replaced.',
        'de': 'sandbox-ai auf {host} installieren, {how}? Eine Installation dort wird ersetzt.'},
    'Installing sandbox-ai on {host}…': {
        'en': 'Installing sandbox-ai on {host}…',
        'de': 'Installiere sandbox-ai auf {host}…'},
    'Install…': {
        'en': 'Install…',
        'de': 'Installieren…'},
    'Prefix': {
        'en': 'Prefix',
        'de': 'Präfix'},
    'Sections folded': {
        'en': 'Sections folded',
        'de': 'Abschnitte eingeklappt'},
    'Sections unfolded': {
        'en': 'Sections unfolded',
        'de': 'Abschnitte aufgeklappt'},
    'Select a check to read it whole here.': {
        'en': 'Select a check to read it whole here.',
        'de': 'Wählen Sie eine Prüfung, um sie hier ganz zu lesen.'},
    'The check chosen, whole': {
        'en': 'The check chosen, whole',
        'de': 'Die gewählte Prüfung, ganz'},
    'The doctor answered in a form this app does not read: its text is below, as it came.': {
        'en': 'The doctor answered in a form this app does not read: its text is below, as it came.',
        'de': 'Der Doctor antwortete in einer Form, die diese App nicht liest: Sein Text steht unten, wie er kam.'},
    "The doctor's checks": {
        'en': "The doctor's checks",
        'de': 'Die Prüfungen des Doctors'},
    'The signed release, downloaded by the host': {
        'en': 'The signed release, downloaded by the host',
        'de': 'Die signierte Version, vom Host heruntergeladen'},
    'This machine installs with install.sh (or: sandbox-ai update).': {
        'en': 'This machine installs with install.sh (or: sandbox-ai update).',
        'de': 'Dieser Rechner installiert mit install.sh (oder: sandbox-ai update).'},
    "This machine's copy ({version})": {
        'en': "This machine's copy ({version})",
        'de': 'Die Kopie dieses Rechners ({version})'},
    'This sandbox-ai cannot install on hosts yet: update it.': {
        'en': 'This sandbox-ai cannot install on hosts yet: update it.',
        'de': 'Dieses sandbox-ai kann noch nicht auf Hosts installieren: Aktualisieren Sie es.'},
    'Used last': {
        'en': 'Used last',
        'de': 'Zuletzt genutzt'},
    'What was found: {detail}': {
        'en': 'What was found: {detail}',
        'de': 'Gefunden: {detail}'},
    'Where': {
        'en': 'Where',
        'de': 'Wo'},
    'Whether the update runs with sudo (a system installation needs it)': {
        'en': 'Whether the update runs with sudo (a system installation needs it)',
        'de': 'Ob die Aktualisierung mit sudo läuft (eine Systeminstallation braucht es)'},
    'as root': {
        'en': 'as root',
        'de': 'als root'},
    'as root (the SSH user is root)': {
        'en': 'as root (the SSH user is root)',
        'de': 'als root (der SSH-Benutzer ist root)'},
    'for every user under {prefix}': {
        'en': 'for every user under {prefix}',
        'de': 'für jeden Benutzer unter {prefix}'},
    'for the SSH user': {
        'en': 'for the SSH user',
        'de': 'für den SSH-Benutzer'},
    'from the signed release ({version})': {
        'en': 'from the signed release ({version})',
        'de': 'aus der signierten Version ({version})'},
    "from this machine's copy": {
        'en': "from this machine's copy",
        'de': 'aus der Kopie dieses Rechners'},
    'sudo as set for the host': {
        'en': 'sudo as set for the host',
        'de': 'sudo wie für den Host eingestellt'},
    'sudo as set for the host ({how})': {
        'en': 'sudo as set for the host ({how})',
        'de': 'sudo wie für den Host eingestellt ({how})'},
    'the lists of this page too': {
        'en': 'the lists of this page too',
        'de': 'auch die Listen dieser Seite'},
    'the newest': {
        'en': 'the newest',
        'de': 'die neueste'},
    'the newest (or X.Y.Z)': {
        'en': 'the newest (or X.Y.Z)',
        'de': 'die neueste (oder X.Y.Z)'},
    'the strip of icons shows every page: Only icons off shows the sections': {
        'en': 'the strip of icons shows every page: Only icons off shows the sections',
        'de': 'die Symbolleiste zeigt jede Seite: Nur Symbole aus zeigt die Abschnitte'},
    'with sudo': {
        'en': 'with sudo',
        'de': 'mit sudo'},
    'with sudo (asked there when sudo wants a password)': {
        'en': 'with sudo (asked there when sudo wants a password)',
        'de': 'mit sudo (dort gefragt, wenn sudo ein Passwort will)'},
    'without sudo': {
        'en': 'without sudo',
        'de': 'ohne sudo'},
    'without sudo (refused where root is needed)': {
        'en': 'without sudo (refused where root is needed)',
        'de': 'ohne sudo (abgelehnt, wo root nötig ist)'},
    '{folder}   (used {time})': {
        'en': '{folder}   (used {time})',
        'de': '{folder}   (genutzt {time})'},
    '{host}: install': {
        'en': '{host}: install',
        'de': '{host}: Installation'},
    '{host}: installing {how}…': {
        'en': '{host}: installing {how}…',
        'de': '{host}: Installation {how}…'},
    '{host}: not installed: {error}': {
        'en': '{host}: not installed: {error}',
        'de': '{host}: nicht installiert: {error}'},
    '{host}: sandbox-ai {before} -> {after}': {
        'en': '{host}: sandbox-ai {before} -> {after}',
        'de': '{host}: sandbox-ai {before} -> {after}'},
}
