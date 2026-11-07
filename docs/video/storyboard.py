"""The storyboard of the sandbox-ai learning video: chapters, scenes and every text in English and German.

This file is data only (standard library, no rendering), so that it can be read by the generator
(docs/video/make_video.py), by the test (tests/test_video_storyboard.py) and by a person who updates the course
when a feature changes. Change the texts here; the generator computes the timing from them.

Structure
---------
COURSE      the title card and the closing card.
CHAPTERS    a list of chapters. Each has an "id" (the test checks the list of ids), a "title", "topics" for its
            title card and "scenes". Every scene has a "kind", a "heading" and "captions" (the narration shown
            as subtitles: one cue each, burned into the video and written to the .vtt files).
LIVE_SETUP  what the generator prepares in a throwaway HOME before it runs the commands marked "live".
STILLS      scenes the generator saves as PNG stills with --stills.

A text is T(english, german). Text in `backticks` is drawn in the monospace font (commands, options, paths).

Scene kinds
-----------
bullets    "bullets": a list of texts, revealed one after the other; optional "note".
table      "columns" and "rows" (cells are texts or plain strings).
compare    "left" and "right": {"title": text, "items": [texts]} (what is shared / what stays outside).
terminal   "steps": commands typed into a terminal. A step has "cmd" and either "live": True (the generator runs
           it in an isolated HOME and shows its real output; "pick" chooses keys of a JSON plan, "head" limits
           the lines) or "out": the lines to show, with "example": True when they are not real output of this
           version. Commands without output show only the command. "comment" adds a "# ..." line before it.
diagram    "diagram": a key of DIAGRAMS: boxes and arrows in a 100 x 40 grid, revealed by their "step".
app        "page": a page of the desktop app (a key of APP_PAGES). The generator uses a real screenshot when
           docs/video/capture_app.py made one, else a schematic of the page; "bullets" go beside it.
"""


def T(en, de):
    """A text in English and German."""
    return {"en": en, "de": de}


LANGUAGES = ("en", "de")

COURSE = {
    "title": T("sandbox-ai", "sandbox-ai"),
    "subtitle": T("The complete course: AI coding agents in a verified sandbox",
                  "Der komplette Kurs: KI-Coding-Agenten in einer geprüften Sandbox"),
    "contents": T("Contents", "Inhalt"),
    "chapter": T("Chapter", "Kapitel"),
    "of": T("of", "von"),
    "in_chapter": T("In this chapter", "In diesem Kapitel"),
    "badges": {
        "live": T("real output", "echte Ausgabe"),
        "example": T("example output", "Beispielausgabe"),
        "mixed": T("real and example output", "echte und Beispielausgabe"),
        "commands": T("commands", "Befehle"),
        "screenshot": T("real app screenshot, staged demo data", "echter App-Screenshot, gestellte Demodaten"),
        "schematic": T("schematic of the app page", "Schema der App-Seite"),
        "example_comment": T("example output (a configured Linux host)",
                             "Beispielausgabe (ein eingerichteter Linux-Host)"),
        "picked": T("selected keys of the JSON plan", "ausgewählte Schlüssel des JSON-Plans"),
    },
    "intro_captions": [
        T("Welcome to the sandbox-ai course. In fourteen short chapters you learn the whole platform.",
          "Willkommen zum sandbox-ai-Kurs. In vierzehn kurzen Kapiteln lernen Sie die ganze Plattform kennen."),
        T("Every command shown exists in this version; real output is marked, examples are marked as examples.",
          "Jeder gezeigte Befehl existiert in dieser Version; echte Ausgaben sind markiert, Beispiele ebenso."),
    ],
    "contents_captions": [
        T("From the first start to teams of agents: security, network, files, hardware, extensions and the app.",
          "Vom ersten Start bis zu Agenten-Teams: Sicherheit, Netz, Dateien, Hardware, Erweiterungen und die App."),
    ],
    "outro": {
        "heading": T("Where to go next", "Wie es weitergeht"),
        "bullets": [
            T("QUICKSTART.md: the same path, hands-on, in fifteen minutes",
              "QUICKSTART_DE.md: derselbe Weg zum Mitmachen, in fünfzehn Minuten"),
            T("README.md: every option and every command in detail",
              "README_DE.md: jede Option und jeder Befehl im Detail"),
            T("`sandbox-ai claude --help` and `sandbox-ai doctor` on your own machine",
              "`sandbox-ai claude --help` und `sandbox-ai doctor` auf Ihrem eigenen Rechner"),
            T("The desktop app: Help, Guided tour", "Die Desktop-App: Hilfe, Geführte Tour"),
        ],
        "captions": [
            T("That was the tour. The quickstart and the README take you further, and every command explains itself with --help.",
              "Das war der Rundgang. Schnellstart und README führen weiter, und jeder Befehl erklärt sich mit --help."),
            T("Thank you for watching.", "Danke fürs Zuschauen."),
        ],
    },
}

# The pages of the desktop app (sandbox-ai-app), in the order of its navigation; used for the schematic when no
# screenshot was captured, and by capture_app.py for the page keys.
APP_NAV = [  # the app's own words (its German translation calls the Board "Tafel" and approvals "Freigaben")
    (T("Project", "Projekt"), [("launch", T("Start", "Start")), ("board", T("Board", "Tafel"))]),
    (T("Running", "Laufend"), [("sandboxes", T("Sandboxes & sessions", "Sandboxen & Sitzungen")),
                               ("approvals", T("Approvals", "Freigaben")),
                               ("terminals", T("Terminals", "Terminals"))]),
    (T("Insight", "Einblick"), [("connections", T("Connections", "Verbindungen")),
                                ("activity", T("Activity", "Aktivität")),
                                ("statistics", T("Statistics", "Statistik")), ("files", T("Files", "Dateien"))]),
    (T("Manage", "Verwalten"), [("kits", T("Kits & plugins", "Kits & Plugins")),
                                ("mcp", T("MCP servers", "MCP-Server")), ("images", T("Images", "Images")),
                                ("credentials", T("Credentials", "Zugangsdaten"))]),
    (T("System", "System"), [("system", T("System", "System")), ("hosts", T("Hosts", "Hosts"))]),
]
APP_PAGES = {key: title for _, pages in APP_NAV for key, title in pages}

# Prepared in a throwaway HOME (shown as /home/me) before the live commands run. {HOME} is that HOME.
LIVE_SETUP = [
    {"mkdir": "~/src/shop"},
    {"mkdir": "~/src/lib"},
    {"mkdir": "~/data"},
    {"write": "~/rules.json", "text": '{"version": 1, "http": [\n'
                                       '  {"id": "no-gists", "host": "api.github.com", "path": "/gists/**", '
                                       '"decision": "deny"},\n'
                                       '  {"id": "releases", "host": "api.github.com", "methods": "POST", '
                                       '"path": "/repos/*/*/releases", "decision": "ask"}\n]}\n'},
    {"run": ["git", "init", "-q", "-b", "main", "."], "cwd": "~/src/shop"},
    {"run": ["git", "config", "user.email", "me@example.org"], "cwd": "~/src/shop"},
    {"run": ["git", "config", "user.name", "Me"], "cwd": "~/src/shop"},
    {"write": "~/src/shop/README.md", "text": "# shop\n"},
    {"run": ["git", "add", "."], "cwd": "~/src/shop"},
    {"run": ["git", "commit", "-q", "-m", "start"], "cwd": "~/src/shop"},
    {"run": ["git", "remote", "add", "origin", "https://github.com/me/shop.git"], "cwd": "~/src/shop"},
    {"write": "~/src/shop/config.env", "text": "DATA_DIR={HOME}/private/data\nPORT=8080\n"
                                               "AWS_KEY=AKIA" "IOSFODNN7EXAMPLE\n"},  # AWS's documented example key, split for the push check
    {"run": ["git", "add", "config.env"], "cwd": "~/src/shop"},
    {"run": ["git", "commit", "-q", "-m", "config"], "cwd": "~/src/shop"},
]

DRY_PLAN = ["agent", "backend", "mode.name", "mode.origin", "workspaces", "https_hosts", "name", "keep",
            "proxy.enabled", "canaries.categories"]

# --- diagrams: boxes (x, y, w, h in a 100 x 40 grid) and arrows; "step" orders their appearance ---------------
DIAGRAMS = {
    "layers": {
        "boxes": [
            {"id": "host", "x": 1, "y": 0, "w": 98, "h": 40, "style": "outer", "align": "top", "step": 0,
             "label": T("Your computer: the host user, never root", "Ihr Rechner: der Host-Benutzer, nie root")},
            {"id": "pod", "x": 3, "y": 6, "w": 66, "h": 32, "style": "layer1", "align": "top", "step": 1,
             "label": T("Layer 1: rootless Podman pod or Bubblewrap", "Schicht 1: rootless Podman-Pod oder Bubblewrap")},
            {"id": "jail", "x": 6, "y": 13, "w": 60, "h": 23, "style": "layer2", "align": "top", "step": 2,
             "label": T("Layer 2: Firejail", "Schicht 2: Firejail"),
             "sub": T("own mount and PID namespaces · no capabilities · NoNewPrivs · Seccomp",
                      "eigene Mount- und PID-Namespaces · keine Capabilities · NoNewPrivs · Seccomp")},
            {"id": "agent", "x": 9, "y": 23, "w": 26, "h": 10, "style": "agent", "step": 3,
             "label": T("Agent", "Agent"), "sub": T("Claude Code · Codex · OpenCode", "Claude Code · Codex · OpenCode")},
            {"id": "work", "x": 39, "y": 23, "w": 24, "h": 10, "style": "ok", "step": 3,
             "label": T("/work", "/work"), "sub": T("your project folder", "Ihr Projektordner")},
            {"id": "guard", "x": 72, "y": 6, "w": 25, "h": 13, "style": "warn", "step": 4,
             "label": T("Guard", "Guard"),
             "sub": T("checks both layers before the agent starts; any failure aborts",
                      "prüft beide Schichten vor dem Agentstart; jeder Fehler bricht ab")},
            {"id": "nft", "x": 72, "y": 22, "w": 25, "h": 14, "style": "bad", "step": 5,
             "label": T("nftables: default deny", "nftables: alles gesperrt"),
             "sub": T("only the agent's provider on TCP/443, unless you widen it",
                      "nur der Provider des Agenten auf TCP/443, außer Sie erweitern es")},
        ],
        "arrows": [
            {"from": "agent", "to": "work", "both": True, "step": 3},
            {"from": "guard", "to": "pod", "style": "warn", "step": 4},
            {"from": "jail", "to": "nft", "style": "bad", "step": 5},
        ],
    },
    "proxy": {
        "boxes": [
            {"id": "ns", "x": 0, "y": 1, "w": 79, "h": 38, "style": "outer", "align": "top", "step": 0,
             "label": T("The sandbox's own network namespace", "Der eigene Netz-Namespace der Sandbox")},
            {"id": "agent", "x": 2, "y": 13, "w": 15, "h": 11, "style": "agent", "step": 0,
             "label": T("Agent", "Agent"), "sub": T("curl, git, pip, npm …", "curl, git, pip, npm …")},
            {"id": "nat", "x": 21, "y": 13, "w": 14, "h": 11, "style": "layer2", "step": 1,
             "label": T("nftables", "nftables"), "sub": T("redirects every TCP connection", "leitet jede TCP-Verbindung um")},
            {"id": "proxy", "x": 39, "y": 9, "w": 22, "h": 19, "style": "proxy", "step": 2,
             "label": T("Connection proxy", "Verbindungs-Proxy"),
             "sub": T("opens TLS with a CA made for this run · logs every request · checks every git push",
                      "öffnet TLS mit einer CA nur für diesen Lauf · protokolliert jede Anfrage · prüft jeden git push")},
            {"id": "fw", "x": 64, "y": 13, "w": 13, "h": 11, "style": "layer1", "step": 3,
             "label": T("Allow-list", "Freigabeliste"), "sub": T("options and answers", "Optionen und Antworten")},
            {"id": "ok", "x": 83, "y": 5, "w": 16, "h": 10, "style": "ok", "step": 4,
             "label": T("Allowed", "Erlaubt"), "sub": T("provider, your hosts", "Provider, Ihre Hosts")},
            {"id": "no", "x": 83, "y": 22, "w": 16, "h": 10, "style": "bad", "step": 4,
             "label": T("Everything else", "Alles andere"), "sub": T("a question, or refused", "eine Rückfrage, oder abgelehnt")},
            {"id": "log", "x": 39, "y": 31, "w": 22, "h": 6, "style": "note", "step": 5,
             "label": T("connections-*.jsonl (credentials redacted)", "connections-*.jsonl (Zugangsdaten geschwärzt)")},
        ],
        "arrows": [
            {"from": "agent", "to": "nat", "step": 1},
            {"from": "nat", "to": "proxy", "step": 2},
            {"from": "proxy", "to": "fw", "step": 3},
            {"from": "fw", "to": "ok", "style": "ok", "step": 4},
            {"from": "fw", "to": "no", "style": "bad", "step": 4},
            {"from": "proxy", "to": "log", "style": "dashed", "step": 5},
        ],
    },
    "approval": {
        "boxes": [
            {"id": "ask", "x": 0, "y": 14, "w": 17, "h": 11, "style": "agent", "step": 0,
             "label": T("connect 203.0.113.9:443", "Verbindung 203.0.113.9:443"),
             "sub": T("not allowed by the options", "von den Optionen nicht erlaubt")},
            {"id": "held", "x": 21, "y": 14, "w": 14, "h": 11, "style": "warn", "step": 1,
             "label": T("Held", "Angehalten"), "sub": T("the sandbox waits for you", "die Sandbox wartet auf Sie")},
            {"id": "notify", "x": 40, "y": 2, "w": 22, "h": 9, "style": "layer1", "step": 2,
             "label": T("Desktop notification", "Desktop-Benachrichtigung"),
             "sub": T("Allow · Allow always · Block", "Erlauben · Immer · Blockieren")},
            {"id": "app", "x": 40, "y": 15, "w": 22, "h": 9, "style": "layer1", "step": 2,
             "label": T("App: Approvals page", "App: Seite Freigaben")},
            {"id": "cli", "x": 40, "y": 28, "w": 22, "h": 9, "style": "layer1", "step": 2,
             "label": T("sandbox-ai approvals", "sandbox-ai approvals")},
            {"id": "once", "x": 68, "y": 0, "w": 15, "h": 8, "style": "ok", "step": 3,
             "label": T("once", "once"), "sub": T("this connection", "diese Verbindung")},
            {"id": "run", "x": 68, "y": 10, "w": 15, "h": 8, "style": "ok", "step": 3,
             "label": T("run", "run"), "sub": T("until the sandbox ends", "bis die Sandbox endet")},
            {"id": "always", "x": 68, "y": 20, "w": 15, "h": 8, "style": "ok", "step": 3,
             "label": T("always", "always"), "sub": T("this project, every start", "dieses Projekt, jeder Start")},
            {"id": "deny", "x": 68, "y": 30, "w": 15, "h": 8, "style": "bad", "step": 3,
             "label": T("deny / block", "ablehnen / blockieren")},
            {"id": "timeout", "x": 17, "y": 30, "w": 22, "h": 8, "style": "warn", "step": 4,
             "label": T("No answer in 60 s: refused", "Keine Antwort in 60 s: abgelehnt")},
        ],
        "arrows": [
            {"from": "ask", "to": "held", "step": 1},
            {"from": "held", "to": "notify", "sides": "h", "step": 2},
            {"from": "held", "to": "app", "step": 2},
            {"from": "held", "to": "cli", "sides": "h", "step": 2},
            {"from": "app", "to": "once", "style": "ok", "sides": "h", "step": 3},
            {"from": "app", "to": "run", "style": "ok", "sides": "h", "step": 3},
            {"from": "app", "to": "always", "style": "ok", "sides": "h", "step": 3},
            {"from": "app", "to": "deny", "style": "bad", "sides": "h", "step": 3},
            {"from": "held", "to": "timeout", "style": "warn", "step": 4},
        ],
    },
    "tokens": {
        "boxes": [
            {"id": "keyring", "x": 0, "y": 2, "w": 22, "h": 10, "style": "outer", "step": 0,
             "label": T("Your keyring", "Ihr Schlüsselbund"), "sub": T("sandbox-ai secrets", "sandbox-ai secrets")},
            {"id": "logins", "x": 0, "y": 18, "w": 22, "h": 14, "style": "outer", "step": 1,
             "label": T("git-logins, publish-logins, identities", "git-logins, publish-logins, identities"),
             "sub": T("servers, users, secret names: never a token", "Server, Benutzer, Secret-Namen: nie ein Token")},
            {"id": "sandbox", "x": 26, "y": 1, "w": 33, "h": 25, "style": "layer1", "align": "top", "step": 2,
             "label": T("Sandbox", "Sandbox")},
            {"id": "tool", "x": 29, "y": 8, "w": 21, "h": 12, "style": "agent", "step": 2,
             "label": T("git, twine, npm …", "git, twine, npm …"),
             "sub": T("no token, or a placeholder", "kein Token, oder ein Platzhalter")},
            {"id": "proxy", "x": 53, "y": 8, "w": 20, "h": 12, "style": "proxy", "step": 3,
             "label": T("Connection proxy", "Verbindungs-Proxy"),
             "sub": T("its own user; adds the token on the wire", "eigener Benutzer; setzt das Token auf der Leitung ein")},
            {"id": "server", "x": 79, "y": 8, "w": 20, "h": 12, "style": "ok", "step": 4,
             "label": T("Git server, registry", "Git-Server, Registry"),
             "sub": T("only the one whose certificate was checked", "nur der mit geprüftem Zertifikat")},
            {"id": "note", "x": 26, "y": 31, "w": 73, "h": 8, "style": "note", "step": 5,
             "label": T("Publishing is asked first (--publish); answers are scrubbed of the token",
                        "Veröffentlichen wird vorher erfragt (--publish); Antworten werden vom Token bereinigt")},
        ],
        "arrows": [
            {"from": "keyring", "to": "logins", "style": "dashed", "step": 1},
            {"from": "logins", "to": "proxy", "sides": "rb", "label": T("token: only here", "Token: nur hier"), "step": 3},
            {"from": "tool", "to": "proxy", "step": 3},
            {"from": "proxy", "to": "server", "style": "ok", "step": 4},
        ],
    },
    "lifecycle": {
        "boxes": [
            {"id": "start", "x": 0, "y": 15, "w": 16, "h": 10, "style": "agent", "step": 0,
             "label": T("sandbox-ai claude", "sandbox-ai claude"), "sub": T("in a terminal", "im Terminal")},
            {"id": "running", "x": 22, "y": 15, "w": 17, "h": 10, "style": "ok", "step": 1,
             "label": T("running", "läuft"), "sub": T("tmux: agent + shell", "tmux: agent + shell")},
            {"id": "paused", "x": 22, "y": 0, "w": 17, "h": 8, "style": "warn", "step": 2,
             "label": T("paused", "pausiert"), "sub": T("every process frozen", "alle Prozesse eingefroren")},
            {"id": "detached", "x": 22, "y": 31, "w": 17, "h": 8, "style": "layer1", "step": 2,
             "label": T("detached", "gelöst"), "sub": T("the agent works on", "der Agent arbeitet weiter")},
            {"id": "stopped", "x": 47, "y": 15, "w": 19, "h": 10, "style": "layer2", "step": 3,
             "label": T("stopped, kept", "beendet, aufbewahrt"), "sub": T("HOME, options, logs", "HOME, Optionen, Logs")},
            {"id": "reborn", "x": 75, "y": 15, "w": 24, "h": 10, "style": "ok", "step": 4,
             "label": T("reborn", "wiedergeboren"),
             "sub": T("new sandbox, same HOME: the agent continues", "neue Sandbox, gleiches HOME: der Agent macht weiter")},
            {"id": "deleted", "x": 47, "y": 31, "w": 19, "h": 8, "style": "bad", "step": 5,
             "label": T("deleted", "gelöscht"), "sub": T("with its HOME", "mit seinem HOME")},
        ],
        "arrows": [
            {"from": "start", "to": "running", "step": 1},
            {"from": "running", "to": "paused", "label": T("pause", "pause"), "offset": -2.5, "step": 2},
            {"from": "paused", "to": "running", "label": T("resume", "resume"), "offset": -2.5, "step": 2},
            {"from": "running", "to": "detached", "label": T("Ctrl-b d", "Ctrl-b d"), "offset": -2.5, "step": 2},
            {"from": "detached", "to": "running", "label": T("attach", "attach"), "offset": -2.5, "step": 2},
            {"from": "running", "to": "stopped", "label": T("stop", "stop"), "step": 3},
            {"from": "stopped", "to": "reborn", "label": T("resume", "resume"), "style": "ok", "step": 4},
            {"from": "stopped", "to": "deleted", "label": T("delete --yes", "delete --yes"), "style": "bad", "step": 5},
        ],
    },
    "mounts": {
        "boxes": [
            {"id": "host", "x": 0, "y": 0, "w": 38, "h": 40, "style": "outer", "align": "top", "step": 0,
             "label": T("Host", "Host")},
            {"id": "shop", "x": 2, "y": 7, "w": 34, "h": 6, "style": "layer1", "step": 0, "label": T("~/src/shop", "~/src/shop")},
            {"id": "lib", "x": 2, "y": 16, "w": 34, "h": 6, "style": "layer1", "step": 1, "label": T("~/src/lib", "~/src/lib")},
            {"id": "data", "x": 2, "y": 25, "w": 34, "h": 6, "style": "layer1", "step": 2, "label": T("~/data", "~/data")},
            {"id": "state", "x": 2, "y": 33, "w": 34, "h": 6, "style": "layer1", "step": 3,
             "label": T("the start's HOME", "das HOME des Starts"),
             "sub": T("~/.local/state/sandbox-ai/sessions/ID/home", "~/.local/state/sandbox-ai/sessions/ID/home")},
            {"id": "sb", "x": 60, "y": 0, "w": 40, "h": 40, "style": "layer2", "align": "top", "step": 0,
             "label": T("Sandbox", "Sandbox")},
            {"id": "work", "x": 62, "y": 7, "w": 36, "h": 6, "style": "ok", "step": 0, "label": T("/work  (rw)", "/work  (rw)")},
            {"id": "wlib", "x": 62, "y": 16, "w": 36, "h": 6, "style": "ok", "step": 1,
             "label": T("/workspaces/lib", "/workspaces/lib")},
            {"id": "mnt", "x": 62, "y": 25, "w": 36, "h": 6, "style": "warn", "step": 2,
             "label": T("/mnt/data  (read-only)", "/mnt/data  (schreibgeschützt)")},
            {"id": "root", "x": 62, "y": 33, "w": 36, "h": 6, "style": "layer1", "step": 3,
             "label": T("/root: the kept HOME", "/root: das aufbewahrte HOME")},
        ],
        "arrows": [
            {"from": "shop", "to": "work", "label": T("-w (first)", "-w (erster)"), "step": 0},
            {"from": "lib", "to": "wlib", "label": T("-w (further)", "-w (weitere)"), "step": 1},
            {"from": "data", "to": "mnt", "label": T("-m SRC:DEST:ro", "-m QUELLE:ZIEL:ro"), "step": 2},
            {"from": "state", "to": "root", "label": T("kept per start", "je Start aufbewahrt"), "step": 3},
        ],
    },
    "memory": {
        "boxes": [
            {"id": "top", "x": 0, "y": 0, "w": 100, "h": 7, "style": "note", "step": 0,
             "label": T("Defaults: 8g memory, half the CPUs, 4096 processes (sandbox-ai limits changes them while it runs)",
                        "Vorgaben: 8g Speicher, die Hälfte der CPUs, 4096 Prozesse (sandbox-ai limits ändert sie im Lauf)")},
            {"id": "normal", "x": 0, "y": 11, "w": 70, "h": 9, "style": "ok", "step": 1,
             "label": T("0 – 85 %: normal work", "0 – 85 %: normales Arbeiten")},
            {"id": "warn", "x": 70, "y": 11, "w": 15, "h": 9, "style": "warn", "step": 2, "label": T("85 %", "85 %")},
            {"id": "bad", "x": 85, "y": 11, "w": 15, "h": 9, "style": "bad", "step": 3, "label": T("95 %", "95 %")},
            {"id": "warnnote", "x": 0, "y": 25, "w": 48, "h": 13, "style": "warn", "step": 2,
             "label": T("At 85 %: you are told", "Bei 85 %: Sie erfahren es"),
             "sub": T("a desktop notification, memory.jsonl; the agent reads /sandbox/session/memory.json",
                      "Desktop-Benachrichtigung, memory.jsonl; der Agent liest /sandbox/session/memory.json")},
            {"id": "badnote", "x": 52, "y": 25, "w": 48, "h": 13, "style": "bad", "step": 3,
             "label": T("At 95 %: the biggest test or build ends", "Bei 95 %: der größte Test oder Build endet"),
             "sub": T("never sandbox-ai's own processes or the agent; the run goes on",
                      "nie die Prozesse von sandbox-ai oder der Agent; der Lauf geht weiter")},
        ],
        "arrows": [],
    },
    "mcp": {
        "boxes": [
            {"id": "asb", "x": 0, "y": 3, "w": 30, "h": 34, "style": "layer1", "align": "top", "step": 0,
             "label": T("Agent sandbox", "Agent-Sandbox")},
            {"id": "agent", "x": 3, "y": 11, "w": 24, "h": 9, "style": "agent", "step": 0, "label": T("Agent", "Agent")},
            {"id": "bridge", "x": 3, "y": 24, "w": 24, "h": 9, "style": "layer2", "step": 0,
             "label": T("stdio bridge", "stdio-Brücke")},
            {"id": "sock", "x": 34, "y": 21, "w": 14, "h": 15, "style": "note", "step": 1,
             "label": T("Unix socket", "Unix-Socket"), "sub": T("read-only mount, no network path", "schreibgeschützt, kein Netzweg")},
            {"id": "relay", "x": 52, "y": 19, "w": 16, "h": 18, "style": "proxy", "step": 2,
             "label": T("Relay", "Relay"), "sub": T("mcp rules · questions · mcp-calls.jsonl", "mcp-Regeln · Rückfragen · mcp-calls.jsonl")},
            {"id": "ssb", "x": 72, "y": 3, "w": 28, "h": 34, "style": "layer1", "align": "top", "step": 3,
             "label": T("MCP server sandbox", "MCP-Server-Sandbox")},
            {"id": "server", "x": 75, "y": 11, "w": 22, "h": 10, "style": "ok", "step": 3,
             "label": T("stdio server", "stdio-Server"), "sub": T("one instance per connection", "eine Instanz je Verbindung")},
            {"id": "srvnote", "x": 75, "y": 25, "w": 22, "h": 9, "style": "note", "step": 4,
             "label": T("no credentials; network only if its definition says so",
                        "keine Zugangsdaten; Netz nur, wenn die Definition es sagt")},
        ],
        "arrows": [
            {"from": "agent", "to": "bridge", "both": True, "step": 0},
            {"from": "bridge", "to": "sock", "both": True, "step": 1},
            {"from": "sock", "to": "relay", "both": True, "step": 2},
            {"from": "relay", "to": "server", "both": True, "step": 3},
        ],
    },
    "vpn": {
        "boxes": [
            {"id": "pod", "x": 0, "y": 2, "w": 62, "h": 36, "style": "layer1", "align": "top", "step": 0,
             "label": T("Podman pod: one network namespace", "Podman-Pod: ein Netz-Namespace")},
            {"id": "agent", "x": 3, "y": 12, "w": 23, "h": 13, "style": "agent", "step": 0,
             "label": T("Agent container", "Agent-Container"),
             "sub": T("firewall, proxy and approvals as always", "Firewall, Proxy und Rückfragen wie immer")},
            {"id": "vpn", "x": 32, "y": 12, "w": 27, "h": 13, "style": "proxy", "step": 1,
             "label": T("VPN container", "VPN-Container"),
             "sub": T("WireGuard · OpenVPN · Mullvad · Tailscale · Tor", "WireGuard · OpenVPN · Mullvad · Tailscale · Tor")},
            {"id": "kill", "x": 32, "y": 29, "w": 27, "h": 7, "style": "bad", "step": 2,
             "label": T("kill switch: nothing else leaves (IPv4, IPv6)", "Kill-Switch: sonst geht nichts hinaus (IPv4, IPv6)")},
            {"id": "tunnel", "x": 67, "y": 12, "w": 14, "h": 13, "style": "ok", "step": 3, "label": T("Tunnel", "Tunnel")},
            {"id": "net", "x": 86, "y": 12, "w": 13, "h": 13, "style": "outer", "step": 3, "label": T("Internet", "Internet")},
            {"id": "check", "x": 65, "y": 29, "w": 34, "h": 8, "style": "warn", "step": 4,
             "label": T("Checked before the agent starts: exit address and DNS",
                        "Geprüft vor dem Agentstart: Ausgangsadresse und DNS")},
        ],
        "arrows": [
            {"from": "agent", "to": "vpn", "step": 1},
            {"from": "vpn", "to": "tunnel", "style": "ok", "step": 3},
            {"from": "tunnel", "to": "net", "style": "ok", "step": 3},
        ],
    },
    "team": {
        "boxes": [
            {"id": "board", "x": 33, "y": 0, "w": 34, "h": 12, "style": "proxy", "step": 0,
             "label": T("Task board: MCP server team-app", "Aufgabenbrett: MCP-Server team-app"),
             "sub": T("own sandbox, no network · post · wait_for_task · done · board",
                      "eigene Sandbox, kein Netz · post · wait_for_task · done · board")},
            {"id": "lead", "x": 2, "y": 20, "w": 28, "h": 10, "style": "ok", "step": 1,
             "label": T("app-claude: the lead", "app-claude: die Leitung"), "sub": T("splits the work", "teilt die Arbeit auf")},
            {"id": "m2", "x": 36, "y": 20, "w": 28, "h": 10, "style": "agent", "step": 2,
             "label": T("app-codex", "app-codex"), "sub": T("waits for tasks, reports back", "wartet auf Aufgaben, meldet zurück")},
            {"id": "m3", "x": 70, "y": 20, "w": 28, "h": 10, "style": "agent", "step": 2,
             "label": T("app-opencode", "app-opencode"), "sub": T("waits for tasks, reports back", "wartet auf Aufgaben, meldet zurück")},
            {"id": "ws", "x": 2, "y": 34, "w": 96, "h": 6, "style": "note", "step": 3,
             "label": T("one sandbox per member, one shared workspace: /work = ~/src/app",
                        "eine Sandbox je Mitglied, ein gemeinsamer Workspace: /work = ~/src/app")},
        ],
        "arrows": [
            {"from": "lead", "to": "board", "both": True, "step": 1},
            {"from": "m2", "to": "board", "both": True, "step": 2},
            {"from": "m3", "to": "board", "both": True, "step": 2},
        ],
    },
    "web": {
        "boxes": [
            {"id": "browser", "x": 0, "y": 2, "w": 18, "h": 10, "style": "agent", "step": 0,
             "label": T("Browser", "Browser"), "sub": T("password + TOTP", "Passwort + TOTP")},
            {"id": "app", "x": 0, "y": 28, "w": 18, "h": 10, "style": "agent", "step": 0,
             "label": T("Desktop app", "Desktop-App"), "sub": T("sandbox-ai-app", "sandbox-ai-app")},
            {"id": "web", "x": 23, "y": 2, "w": 22, "h": 10, "style": "proxy", "step": 1,
             "label": T("sandbox-ai web", "sandbox-ai web"), "sub": T("HTTPS · CSRF · audit log", "HTTPS · CSRF · Audit-Log")},
            {"id": "cmd", "x": 29, "y": 15, "w": 24, "h": 10, "style": "layer2", "step": 1,
             "label": T("sandbox-ai commands", "sandbox-ai-Befehle"), "sub": T("every action is one command", "jede Aktion ist ein Befehl")},
            {"id": "local", "x": 61, "y": 2, "w": 17, "h": 9, "style": "ok", "step": 2,
             "label": T("local", "local"), "sub": T("this machine", "dieser Rechner")},
            {"id": "ct1", "x": 61, "y": 15, "w": 17, "h": 10, "style": "layer1", "step": 2,
             "label": T("ct1 (Proxmox LXC)", "ct1 (Proxmox-LXC)"), "sub": T("over SSH", "über SSH")},
            {"id": "gpu", "x": 61, "y": 29, "w": 17, "h": 9, "style": "layer1", "step": 2,
             "label": T("gpu-box", "gpu-box"), "sub": T("over SSH", "über SSH")},
            {"id": "note", "x": 82, "y": 8, "w": 18, "h": 24, "style": "note", "step": 3,
             "label": T("Hosts need only sshd and sandbox-ai; host keys are checked strictly",
                        "Hosts brauchen nur sshd und sandbox-ai; Host-Schlüssel werden streng geprüft")},
        ],
        "arrows": [
            {"from": "browser", "to": "web", "step": 1},
            {"from": "app", "to": "cmd", "sides": "h", "step": 1},
            {"from": "web", "to": "cmd", "step": 1},
            {"from": "cmd", "to": "local", "sides": "h", "step": 2},
            {"from": "cmd", "to": "ct1", "label": T("ssh", "ssh"), "step": 2},
            {"from": "cmd", "to": "gpu", "label": T("ssh", "ssh"), "sides": "h", "step": 2},
        ],
    },
}

# --- the chapters ---------------------------------------------------------------------------------------------
CHAPTERS = [
    {
        "id": "what-why",
        "title": T("What sandbox-ai is, and why", "Was sandbox-ai ist, und warum"),
        "topics": [T("Agents in a verified sandbox", "Agenten in einer geprüften Sandbox"),
                   T("Two layers: Podman or Bubblewrap, Firejail inside", "Zwei Schichten: Podman oder Bubblewrap, darin Firejail"),
                   T("What is shared with the host", "Was mit dem Host geteilt wird")],
        "scenes": [
            {"kind": "bullets", "heading": T("Why a sandbox?", "Warum eine Sandbox?"),
             "bullets": [
                 T("An agent with a shell runs whatever it decides is a good idea, with your user's rights",
                   "Ein Agent mit Shell führt aus, was er für eine gute Idee hält, mit den Rechten Ihres Benutzers"),
                 T("It could read `~/.ssh`, push to any remote, send your code to any host",
                   "Er könnte `~/.ssh` lesen, zu jedem Remote pushen, Ihren Code an jeden Host schicken"),
                 T("sandbox-ai gives it the project folder and the destinations you name, nothing else",
                   "sandbox-ai gibt ihm den Projektordner und die Ziele, die Sie nennen, sonst nichts"),
                 T("Claude Code, Codex and OpenCode as they are: own logins, plugins, MCP servers",
                   "Claude Code, Codex und OpenCode, wie sie sind: eigene Logins, Plugins, MCP-Server"),
             ],
             "captions": [
                 T("An AI agent with shell access runs whatever it thinks is a good idea, with all the rights of your user.",
                   "Ein KI-Agent mit Shell-Zugriff führt aus, was er für eine gute Idee hält, mit allen Rechten Ihres Benutzers."),
                 T("sandbox-ai runs Claude Code, Codex or OpenCode so that they reach only your project and the hosts you name.",
                   "sandbox-ai führt Claude Code, Codex oder OpenCode so aus, dass sie nur Ihr Projekt und die genannten Hosts erreichen."),
             ]},
            {"kind": "diagram", "diagram": "layers", "heading": T("Two layers, verified before every start",
                                                                   "Zwei Schichten, vor jedem Start geprüft"),
             "captions": [
                 T("The outer layer is a rootless Podman pod or Bubblewrap; inside it, Firejail confines the agent once more.",
                   "Die äußere Schicht ist ein rootless Podman-Pod oder Bubblewrap; darin schränkt Firejail den Agenten noch einmal ein."),
                 T("A guard checks namespaces, capabilities, NoNewPrivs and Seccomp. Any failed check aborts: there is no silent fallback.",
                   "Ein Guard prüft Namespaces, Capabilities, NoNewPrivs und Seccomp. Jede fehlgeschlagene Prüfung bricht ab, ohne stillen Rückfall."),
                 T("Outbound traffic is default-deny: only the agent's own provider is open, on TCP port 443.",
                   "Ausgehender Verkehr ist gesperrt: offen ist nur der eigene Provider des Agenten, auf TCP-Port 443."),
             ]},
            {"kind": "compare", "heading": T("What the sandbox shares with your computer", "Was die Sandbox mit Ihrem Rechner teilt"),
             "left": {"title": T("Shared", "Geteilt"), "items": [
                 T("your project folder at `/work`", "Ihr Projektordner unter `/work`"),
                 T("further workspaces at `/workspaces/NAME`", "weitere Workspaces unter `/workspaces/NAME`"),
                 T("a private HOME, kept per start", "ein eigenes HOME, je Start aufbewahrt"),
                 T("a copy of the agent's login", "eine Kopie des Agenten-Logins"),
                 T("the provider's hosts on TCP/443", "die Hosts des Providers auf TCP/443"),
             ]},
             "right": {"title": T("Stays outside", "Bleibt draußen"), "items": [
                 T("your HOME: `~/.ssh`, `~/.config`, `~/.gnupg` …", "Ihr HOME: `~/.ssh`, `~/.config`, `~/.gnupg` …"),
                 T("Git and API tokens: they stay in the proxy", "Git- und API-Tokens: sie bleiben im Proxy"),
                 T("the host's loopback services", "die Loopback-Dienste des Hosts"),
                 T("sandbox-ai itself (only `runtime.py`, read-only)", "sandbox-ai selbst (nur `runtime.py`, schreibgeschützt)"),
                 T("every other destination on the network", "jedes andere Ziel im Netz"),
             ]},
             "captions": [
                 T("Inside, your project is /work. The agent gets a private HOME and a copy of its login, nothing else of your home directory.",
                   "Drinnen ist Ihr Projekt /work. Der Agent bekommt ein eigenes HOME und eine Kopie seines Logins, sonst nichts aus Ihrem Home."),
                 T("SSH keys, tokens, other projects and the host's own services stay outside.",
                   "SSH-Schlüssel, Tokens, andere Projekte und die Dienste des Hosts bleiben draußen."),
             ]},
            {"kind": "bullets", "heading": T("When to use something else", "Wann etwas anderes passt"),
             "bullets": [
                 T("Protection against kernel exploits: use a virtual machine", "Schutz vor Kernel-Exploits: nehmen Sie eine VM"),
                 T("Windows, or no rootless Podman and no Bubblewrap", "Windows, oder weder rootless Podman noch Bubblewrap"),
                 T("macOS works through a rootless `podman machine`, so far with limits",
                   "macOS geht über eine rootless `podman machine`, bisher mit Einschränkungen"),
             ],
             "note": T("sandbox-ai makes confinement checkable rather than promised.",
                       "sandbox-ai macht die Einschränkung prüfbar statt nur versprochen."),
             "captions": [
                 T("A sandbox shares the host's kernel. If you need protection against kernel exploits, use a virtual machine.",
                   "Eine Sandbox teilt sich den Kernel des Hosts. Wer Schutz vor Kernel-Exploits braucht, nimmt eine VM."),
             ]},
        ],
    },
    {
        "id": "install",
        "title": T("Install and check the host", "Installieren und den Host prüfen"),
        "topics": [T("install.sh, .pyz, AppImage, macOS app", "install.sh, .pyz, AppImage, macOS-App"),
                   T("sandbox-ai doctor", "sandbox-ai doctor"), T("Images and logins", "Images und Logins")],
        "scenes": [
            {"kind": "table", "heading": T("Four ways to install", "Vier Wege zur Installation"),
             "columns": [T("Way", "Weg"), T("For", "Für"), T("Updates", "Updates")], "widths": [0.27, 0.43, 0.30],
             "rows": [
                 ["`install.sh`", T("command line and app (recommended)", "Kommandozeile und App (empfohlen)"),
                  T("`sandbox-ai update`, signed", "`sandbox-ai update`, signiert")],
                 ["`pip install sandbox-ai`", T("the command line in a Python environment", "die Kommandozeile in einer Python-Umgebung"),
                  "`pip install -U sandbox-ai`"],
                 ["`sandbox-ai.pyz`", T("one file, any Python 3.9+", "eine Datei, jedes Python 3.9+"),
                  T("download the new file", "neue Datei laden")],
                 [T("AppImage / `sandbox-ai.app`", "AppImage / `sandbox-ai.app`"),
                  T("the app with its own Python and Qt (Linux, macOS 14+)", "die App mit eigenem Python und Qt (Linux, macOS 14+)"),
                  T("download the new file", "neue Datei laden")],
             ],
             "captions": [
                 T("Every way runs the sandboxes the same way. install.sh is recommended: it installs the command line and the app and updates signed releases.",
                   "Jeder Weg betreibt die Sandboxes gleich. Empfohlen ist install.sh: Es installiert Kommandozeile und App und aktualisiert signierte Releases."),
                 T("The .pyz is one file for any Python 3.9 or newer; the AppImage and the macOS app bring the desktop app with everything inside.",
                   "Die .pyz ist eine Datei für jedes Python ab 3.9; AppImage und macOS-App bringen die Desktop-App mit allem darin."),
             ]},
            {"kind": "terminal", "heading": T("Installing", "Installieren"),
             "steps": [
                 {"comment": T("from a checkout: commands in ~/.local/bin, PATH in ~/.bashrc",
                               "aus einem Checkout: Befehle in ~/.local/bin, PATH in ~/.bashrc"),
                  "cmd": "./install.sh --from ."},
                 {"comment": T("later: the newest signed release", "später: das neueste signierte Release"),
                  "cmd": "sandbox-ai update --check"},
                 {"comment": T("or one file, nothing installed (not into a project folder)",
                               "oder eine Datei, nichts installiert (nicht in einen Projektordner)"),
                  "cmd": "curl -fLO https://github.com/c0decave/sandbox-ai/releases/latest/download/sandbox-ai.pyz"},
                 {"cmd": "./sandbox-ai.pyz doctor"},
                 {"cmd": "sandbox-ai version", "live": True},
             ],
             "captions": [
                 T("install.sh puts the commands into ~/.local/bin and adds that folder to your PATH in a marked block of ~/.bashrc and ~/.zshrc.",
                   "install.sh legt die Befehle nach ~/.local/bin und trägt den Ordner in einem markierten Block von ~/.bashrc und ~/.zshrc in den PATH ein."),
                 T("Updates check the release signature before anything is unpacked. Never keep the launcher inside a project: an agent could rewrite it.",
                   "Updates prüfen die Release-Signatur, bevor etwas entpackt wird. Den Starter nie in ein Projekt legen: ein Agent könnte ihn ändern."),
             ]},
            {"kind": "terminal", "heading": T("sandbox-ai doctor: what is missing, and the fix", "sandbox-ai doctor: was fehlt, und die Lösung"),
             "steps": [
                 {"cmd": "sandbox-ai doctor --plain", "example": True, "out": [
                     "ok    Ordinary user             me",
                     "ok    Python                    3.12.3",
                     "ok    sandbox-ai                0.1.61 (/home/me/.local/share/sandbox-ai/versions/0.1.61)",
                     "ok    User namespaces           available",
                     "ok    systemd user session      /run/user/1000/bus",
                     "ok    Connection proxy          nftables NAT available",
                     "ok    Podman                    rootless, 5.4.2",
                     "ok    pasta                     /usr/bin/pasta, /dev/net/tun",
                     "ok    Delegated UID/GID ranges  65536 UIDs, 65536 GIDs",
                     "WARN  Image claude              not built",
                     "                                -> sandbox-ai build claude",
                     "ok    Security mode             medium (default); modes: lax, low, medium, high, paranoid",
                 ]},
             ],
             "captions": [
                 T("doctor checks every requirement: rootless Podman, pasta, user namespaces, the proxy's kernel modules, images and logins.",
                   "doctor prüft jede Voraussetzung: rootless Podman, pasta, User-Namespaces, die Kernelmodule des Proxys, Images und Logins."),
                 T("Each problem comes with the command that fixes it. Run it as your ordinary user, never with sudo.",
                   "Zu jedem Problem nennt er den Befehl, der es behebt. Führen Sie ihn als normaler Benutzer aus, nie mit sudo."),
             ]},
            {"kind": "terminal", "heading": T("Images and logins", "Images und Logins"),
             "steps": [
                 {"comment": T("the agent image from the newest release (or: codex, opencode, all)",
                               "das Agent-Image aus dem neuesten Release (oder: codex, opencode, all)"),
                  "cmd": "sandbox-ai build claude"},
                 {"comment": T("a login of the sandboxes' own, kept for all of them",
                               "ein eigener Login der Sandboxes, für alle aufbewahrt"),
                  "cmd": "sandbox-ai login claude"},
                 {"cmd": "sandbox-ai login --status"},
             ],
             "captions": [
                 T("build makes one image per agent from the newest releases; run it again later to update.",
                   "build erzeugt ein Image je Agent aus den neuesten Releases; später erneut ausführen, um zu aktualisieren."),
                 T("If you are logged in on the host, sandboxes get a copy. sandbox-ai login gives them a login of their own instead.",
                   "Sind Sie auf dem Host angemeldet, bekommen Sandboxes eine Kopie. sandbox-ai login gibt ihnen stattdessen einen eigenen Login."),
             ]},
        ],
    },
    {
        "id": "first-start",
        "title": T("The first start", "Der erste Start"),
        "topics": [T("sandbox-ai claude -w DIR", "sandbox-ai claude -w DIR"), T("tmux: detach and attach", "tmux: lösen und verbinden"),
                   T("list, resume, stop, names, kept HOME", "list, resume, stop, Namen, aufbewahrtes HOME")],
        "scenes": [
            {"kind": "terminal", "heading": T("Look before you start: --dry-run", "Erst schauen: --dry-run"),
             "steps": [
                 {"cmd": "sandbox-ai claude -w ~/src/shop --dry-run", "live": True, "pick": DRY_PLAN},
             ],
             "captions": [
                 T("--dry-run prints the plan and starts nothing: the security mode, the workspaces, the open hosts and the start's name.",
                   "--dry-run zeigt den Plan und startet nichts: den Sicherheitsmodus, die Workspaces, die offenen Hosts und den Namen des Starts."),
                 T("By default only the agent's provider is open, the proxy is on, decoy files are planted, and the HOME is kept.",
                   "Standardmäßig ist nur der Provider offen, der Proxy läuft, Köderdateien liegen bereit, und das HOME wird aufbewahrt."),
             ]},
            {"kind": "terminal", "heading": T("Start an agent", "Einen Agenten starten"),
             "steps": [
                 {"cmd": "sandbox-ai claude -w ~/src/shop", "example": True, "out": [
                     "agent-sandbox: auth copied: /home/me/.claude/.credentials.json -> /root/.claude/.credentials.json",
                     "agent-sandbox: Claude access token valid for 7.4 h",
                     "agent-sandbox: claude-shop-3f9a is running. Detach: Ctrl-b d (or close the terminal). "
                     "Re-attach: sandbox-ai attach claude-shop-3f9a",
                 ]},
                 {"comment": T("inside: window 'agent' runs Claude, window 'shell' is bash (Ctrl-b n)",
                               "darin: Fenster 'agent' mit Claude, Fenster 'shell' mit bash (Ctrl-b n)"),
                  "cmd": "sandbox-ai attach claude-shop-3f9a"},
             ],
             "captions": [
                 T("Inside, the project is /work. An interactive start runs in tmux: the window agent and a shell window in the same sandbox.",
                   "Drinnen ist das Projekt /work. Ein interaktiver Start läuft in tmux: das Fenster agent und ein Shell-Fenster in derselben Sandbox."),
                 T("Ctrl-b d or closing the terminal detaches; the agent works on, and attach brings you back.",
                   "Ctrl-b d oder das Schließen des Terminals löst; der Agent arbeitet weiter, und attach holt Sie zurück."),
             ]},
            {"kind": "terminal", "heading": T("Every start has a name", "Jeder Start hat einen Namen"),
             "steps": [
                 {"cmd": "sandbox-ai list", "example": True, "out": [
                     "NAME              KIND     AGENT   STATE    HOME  STARTED (UTC)     WORKSPACE",
                     "claude-shop-3f9a  session  claude  running  kept  2026-11-07 09:12  /home/me/src/shop",
                 ]},
                 {"cmd": "sandbox-ai rename claude-shop-3f9a shop"},
                 {"cmd": "sandbox-ai pause shop"},
                 {"cmd": "sandbox-ai resume shop"},
                 {"cmd": "sandbox-ai stop shop"},
                 {"comment": T("later: reborn, the agent continues its conversation",
                               "später: wiedergeboren, der Agent setzt das Gespräch fort"),
                  "cmd": "sandbox-ai resume shop"},
                 {"cmd": "sandbox-ai list --history shop"},
             ],
             "captions": [
                 T("A start is named AGENT-PROJECT-xxxx, or what you choose with --name. Every command takes the name.",
                   "Ein Start heißt AGENT-PROJEKT-xxxx, oder wie Sie ihn mit --name nennen. Jeder Befehl nimmt den Namen."),
                 T("pause freezes every process; stop ends the sandbox but keeps HOME, options and logs, so resume can bring it back later.",
                   "pause friert alle Prozesse ein; stop beendet die Sandbox, behält aber HOME, Optionen und Logs, damit resume sie später zurückholt."),
             ]},
            {"kind": "diagram", "diagram": "lifecycle", "heading": T("The life of a start", "Das Leben eines Starts"),
             "captions": [
                 T("A stopped start is reborn as a new sandbox with the same options and HOME; the agent continues its last conversation.",
                   "Ein beendeter Start wird als neue Sandbox mit denselben Optionen und demselben HOME wiedergeboren; der Agent setzt sein Gespräch fort."),
                 T("Running processes are not checkpointed: what survives is the HOME and the workspaces.",
                   "Laufende Prozesse werden nicht gesichert: Es überleben das HOME und die Workspaces."),
             ]},
            {"kind": "bullets", "heading": T("The kept HOME", "Das aufbewahrte HOME"),
             "bullets": [
                 T("Kept in `~/.local/state/sandbox-ai/sessions/ID/`: conversations, settings, logins",
                   "Aufbewahrt in `~/.local/state/sandbox-ai/sessions/ID/`: Gespräche, Einstellungen, Logins"),
                 T("Before a rebirth every change to its start files is shown: `sandbox-ai resume shop --review-home`",
                   "Vor einer Wiedergeburt wird jede Änderung an seinen Startdateien gezeigt: `sandbox-ai resume shop --review-home`"),
                 T("`--no-keep` discards the HOME at the end: suits scripts", "`--no-keep` verwirft das HOME am Ende: passt für Skripte"),
                 T("`sandbox-ai gc --older-than 30 --dry-run` lists old kept starts",
                   "`sandbox-ai gc --older-than 30 --dry-run` zeigt alte aufbewahrte Starts"),
             ],
             "captions": [
                 T("A kept HOME is the one thing a compromised agent could use to reach its next start, so every change is named before it comes back.",
                   "Ein aufbewahrtes HOME ist das Einzige, womit ein kompromittierter Agent seinen nächsten Start erreichen könnte; darum wird jede Änderung vorher genannt."),
                 T("With --no-keep nothing carries over. In the paranoid mode no HOME is ever kept.",
                   "Mit --no-keep wird nichts übernommen. Im Modus paranoid wird nie ein HOME aufbewahrt."),
             ]},
        ],
    },
    {
        "id": "network",
        "title": T("Network: allow-list, proxy, approvals", "Netz: Freigaben, Proxy, Rückfragen"),
        "topics": [T("Presets such as --allow-github", "Voreinstellungen wie --allow-github"),
                   T("The connection proxy and DNS", "Der Verbindungs-Proxy und DNS"),
                   T("sandbox-ai net allow; questions: once, run, always", "sandbox-ai net allow; Rückfragen: once, run, always")],
        "scenes": [
            {"kind": "diagram", "diagram": "proxy", "heading": T("Every connection passes the proxy", "Jede Verbindung passiert den Proxy"),
             "captions": [
                 T("nftables redirects every TCP connection of the agent to a proxy inside the sandbox; programs need no proxy settings.",
                   "nftables leitet jede TCP-Verbindung des Agenten zu einem Proxy in der Sandbox um; Programme brauchen keine Proxy-Einstellungen."),
                 T("The proxy opens TLS with a CA made for this run, logs every request with credentials redacted, and holds every git push for the privacy check.",
                   "Der Proxy öffnet TLS mit einer CA nur für diesen Lauf, protokolliert jede Anfrage geschwärzt und hält jeden git push für die Datenschutzprüfung an."),
             ]},
            {"kind": "table", "heading": T("Opening destinations", "Ziele öffnen"),
             "columns": [T("Option", "Option"), T("Opens", "Öffnet")],
             "rows": [
                 ["`--allow-github`  `--allow-gitlab`", T("github.com and its API, raw and release hosts; gitlab.com", "github.com samt API-, Raw- und Release-Hosts; gitlab.com")],
                 ["`--allow-pypi`  `--allow-npm`", T("pypi.org, files.pythonhosted.org; registry.npmjs.org", "pypi.org, files.pythonhosted.org; registry.npmjs.org")],
                 ["`--allow-kernel-org`  `--allow-duckduckgo`", T("kernel.org hosts; DuckDuckGo search", "kernel.org-Hosts; DuckDuckGo-Suche")],
                 ["`--profile development`", T("provider, PyPI, GitHub, OpenAI and Anthropic APIs", "Provider, PyPI, GitHub, OpenAI- und Anthropic-APIs")],
                 ["`--allow-host HOST`", T("one name, pinned, TCP/443", "ein Name, festgelegt, TCP/443")],
                 ["`--allow-tcp IP:PORT`  `--allow-host-port PORT`", T("one port; a port on your own host", "ein Port; ein Port auf Ihrem eigenen Host")],
                 ["`--offline`  `--egress-all-allow`", T("nothing at all; every outbound destination", "gar nichts; jedes ausgehende Ziel")],
             ],
             "captions": [
                 T("Presets open well-known hosts on HTTPS only. --allow-host pins one name; --allow-host-port reaches a service on your own machine, such as a local Git server.",
                   "Voreinstellungen öffnen bekannte Hosts nur über HTTPS. --allow-host legt einen Namen fest; --allow-host-port erreicht einen Dienst auf Ihrem Rechner, etwa einen lokalen Git-Server."),
             ]},
            {"kind": "terminal", "heading": T("Presets in the plan", "Voreinstellungen im Plan"),
             "steps": [
                 {"cmd": "sandbox-ai claude --allow-github --allow-pypi --dry-run", "live": True,
                  "pick": ["https_hosts", "https_networks", "nameservers"]},
             ],
             "captions": [
                 T("Each name is resolved on the host, pinned in the sandbox's /etc/hosts and opened on port 443. There is no DNS inside by default.",
                   "Jeder Name wird auf dem Host aufgelöst, in /etc/hosts der Sandbox festgelegt und auf Port 443 geöffnet. DNS gibt es drinnen standardmäßig nicht."),
                 T("--dns gives real lookups and logs each of them; it is a deliberate widening, because resolvers can carry data out.",
                   "--dns erlaubt echte Abfragen und protokolliert jede; das ist eine bewusste Erweiterung, denn Resolver können Daten hinaustragen."),
             ]},
            {"kind": "terminal", "heading": T("Change a running sandbox", "Eine laufende Sandbox ändern"),
             "steps": [
                 {"cmd": "sandbox-ai net list"},
                 {"cmd": "sandbox-ai net allow --allow-host pypi.org --allow-github"},
                 {"comment": T("a service on this host, through a relay", "ein Dienst auf diesem Host, über ein Relay"),
                  "cmd": "sandbox-ai net allow --allow-host-port 3000"},
                 {"comment": T("and for the next starts too (.sandbox-ai.json)", "und auch für die nächsten Starts (.sandbox-ai.json)"),
                  "cmd": "sandbox-ai net allow --allow-host api.osv.dev --keep"},
                 {"cmd": "sandbox-ai net show"},
                 {"cmd": "sandbox-ai net revoke --allow-host pypi.org"},
             ],
             "captions": [
                 T("sandbox-ai net allow widens a running sandbox without a restart; --keep writes the option into the project file for the next starts.",
                   "sandbox-ai net allow erweitert eine laufende Sandbox ohne Neustart; --keep schreibt die Option für die nächsten Starts in die Projektdatei."),
                 T("Only you can do this: the control socket is hidden from the agent.",
                   "Nur Sie können das: Der Steuer-Socket ist vor dem Agenten verborgen."),
             ]},
            {"kind": "diagram", "diagram": "approval", "heading": T("Ask instead of fail", "Fragen statt scheitern"),
             "captions": [
                 T("A connection the options do not allow becomes a question: on your desktop, in the app, and in sandbox-ai approvals.",
                   "Eine Verbindung, die die Optionen nicht erlauben, wird zur Rückfrage: auf dem Desktop, in der App und in sandbox-ai approvals."),
                 T("Allow it once, for the run, or always for this project. A question nobody answers is refused after sixty seconds.",
                   "Erlauben Sie sie einmal, für den Lauf oder immer für dieses Projekt. Eine unbeantwortete Rückfrage wird nach sechzig Sekunden abgelehnt."),
             ]},
            {"kind": "terminal", "heading": T("Answering questions", "Rückfragen beantworten"),
             "steps": [
                 {"cmd": "sandbox-ai approvals list", "example": True, "out": [
                     "ID               RUN               QUESTION",
                     "1762506000-41-1  claude-shop-3f9a  connect to 203.0.113.9:443 (tls), named api.example.com by the agent "
                     "(not verified); asked by curl (pid 7, /usr/bin/curl)",
                 ]},
                 {"cmd": "sandbox-ai approvals allow 1762506000-41-1 --scope run"},
                 {"cmd": "sandbox-ai approvals history"},
                 {"cmd": "sandbox-ai approvals remembered", "live": True},
             ],
             "captions": [
                 T("Each question names the sandbox and the process behind it. The server name is the agent's claim: what you allow is the address.",
                   "Jede Rückfrage nennt die Sandbox und den Prozess dahinter. Der Servername ist die Behauptung des Agenten: Erlaubt wird die Adresse."),
                 T("Answers given always are remembered per project; remembered lists them, forget removes one.",
                   "Mit always gegebene Antworten werden je Projekt gemerkt; remembered listet sie, forget entfernt eine."),
             ]},
        ],
    },
    {
        "id": "security",
        "title": T("Security modes, canaries, privacy", "Sicherheitsmodi, Köder, Datenschutz"),
        "topics": [T("lax to paranoid, and the taint", "lax bis paranoid, und der Taint"),
                   T("Canary files", "Köderdateien (Canaries)"),
                   T("Privacy check; logins that stay outside", "Datenschutzprüfung; Logins, die draußen bleiben")],
        "scenes": [
            {"kind": "table", "heading": T("Five security modes", "Fünf Sicherheitsmodi"),
             "columns": [T("Mode", "Modus"), T("Network", "Netz"), T("Approvals and more", "Rückfragen und mehr")],
             "widths": [0.14, 0.43, 0.43],
             "rows": [
                 ["`lax`", T("every outbound destination, logged", "jedes ausgehende Ziel, protokolliert"), T("no questions", "keine Rückfragen")],
                 ["`low`", T("provider, PyPI, npm, GitHub, the AI APIs", "Provider, PyPI, npm, GitHub, die KI-APIs"), T("questions on", "Rückfragen an")],
                 ["`medium`", T("the provider only (the default)", "nur der Provider (Standard)"), T("questions on", "Rückfragen an")],
                 ["`high`", T("the provider only; options cannot open more", "nur der Provider; Optionen öffnen nichts"),
                  T("answers at most for the run; writes asked; every push checked", "Antworten höchstens für den Lauf; Schreiben erfragt; jeder Push geprüft")],
                 ["`paranoid`", T("as high; every other HTTP request refused", "wie high; jede andere HTTP-Anfrage abgelehnt"),
                  T("answers once; all workspaces read-only", "Antworten einmal; alle Workspaces schreibgeschützt")],
             ],
             "captions": [
                 T("A security mode is a preset of options, from lax to paranoid. Options may only narrow it; in high and paranoid, one that widens it is refused.",
                   "Ein Sicherheitsmodus ist eine Vorgabe von Optionen, von lax bis paranoid. Optionen dürfen ihn nur verengen; in high und paranoid wird eine erweiternde abgelehnt."),
                 T("The mode comes from --mode, the project file or your defaults; an administrator can set a minimum.",
                   "Der Modus kommt aus --mode, der Projektdatei oder Ihren Vorgaben; ein Administrator kann ein Minimum setzen."),
             ]},
            {"kind": "terminal", "heading": T("A mode in action", "Ein Modus in Aktion"),
             "steps": [
                 {"cmd": "sandbox-ai claude --mode high --allow-github --dry-run", "live": True},
                 {"cmd": "sandbox-ai claude --mode paranoid --dry-run", "live": True,
                  "pick": ["mode.name", "mode.options", "mode.approval_scope", "rulesets[].name", "rulesets[].http"]},
             ],
             "captions": [
                 T("In high, --allow-github is refused, with the reason and the laxer mode that would allow it.",
                   "In high wird --allow-github abgelehnt, mit Begründung und dem laxeren Modus, der es erlauben würde."),
                 T("paranoid adds read-only workspaces, tight limits, answers only once and a ruleset that refuses every HTTP request but the provider's.",
                   "paranoid fügt schreibgeschützte Workspaces, enge Limits, Antworten nur einmal und ein Regelwerk hinzu, das jede HTTP-Anfrage außer zum Provider ablehnt."),
             ]},
            {"kind": "bullets", "heading": T("The run's taint", "Der Taint eines Laufs"),
             "bullets": [
                 T("Untrusted content entering a run taints it", "Nicht vertrauenswürdige Inhalte im Lauf markieren ihn (Taint)"),
                 T("`high`: content from any host but the provider, then every MCP tool call asks",
                   "`high`: Inhalte von jedem Host außer dem Provider, danach fragt jeder MCP-Tool-Aufruf"),
                 T("`paranoid`: MCP results, WebSearch and WebFetch too, then shell, edits and fetches ask",
                   "`paranoid`: auch MCP-Ergebnisse, WebSearch und WebFetch, danach fragen Shell, Edits und Fetches"),
                 T("`sandbox-ai net untaint RUN` takes the taint back", "`sandbox-ai net untaint RUN` nimmt den Taint zurück"),
             ],
             "captions": [
                 T("A prompt injection arrives with content. So the strict modes escalate once untrusted content has entered the run.",
                   "Eine Prompt-Injection kommt mit Inhalten. Darum verschärfen die strengen Modi, sobald nicht vertrauenswürdige Inhalte im Lauf sind."),
             ]},
            {"kind": "bullets", "heading": T("Canary files: decoys that report", "Köderdateien, die sich melden"),
             "bullets": [
                 T("Fake `~/.aws/credentials`, SSH keys, gh, `~/.netrc`, a password list, `~/.kube/config`",
                   "Falsche `~/.aws/credentials`, SSH-Schlüssel, gh, `~/.netrc`, eine Passwortliste, `~/.kube/config`"),
                 T("Tokens made for each run, in the shape of real ones, accepted by no service",
                   "Tokens je Lauf, in der Form echter, die kein Dienst annimmt"),
                 T("A read is reported; a token leaving (proxy, DNS, push) is refused and stops the sandbox",
                   "Ein Lesen wird gemeldet; ein Token, das hinaus will (Proxy, DNS, Push), wird abgelehnt und stoppt die Sandbox"),
                 T("`sandbox-ai canaries list RUN`, `sandbox-ai canaries resume RUN`",
                   "`sandbox-ai canaries list RUN`, `sandbox-ai canaries resume RUN`"),
             ],
             "captions": [
                 T("Nothing legitimate needs a canary. Whoever opens one, and above all whoever sends it somewhere, is worth knowing about.",
                   "Nichts Legitimes braucht einen Köder. Wer einen öffnet, und vor allem wer ihn verschickt, ist es wert, bekannt zu sein."),
             ]},
            {"kind": "terminal", "heading": T("Privacy check before a push", "Datenschutzprüfung vor dem Push"),
             "steps": [
                 {"cmd": "sandbox-ai privacy-check", "live": True},
             ],
             "captions": [
                 T("Before anything is published, sandbox-ai looks for what should stay private: your home path, LAN addresses, tokens.",
                   "Bevor etwas veröffentlicht wird, sucht sandbox-ai nach dem, was privat bleiben soll: Ihrem Home-Pfad, LAN-Adressen, Tokens."),
                 T("It runs after every run, as the agent's Git hooks, by hand, and in the proxy, which refuses a push with blocking findings.",
                   "Sie läuft nach jedem Lauf, als Git-Hooks des Agenten, von Hand, und im Proxy, der einen Push mit blockierenden Funden ablehnt."),
             ]},
            {"kind": "diagram", "diagram": "tokens", "heading": T("Tokens stay out of the sandbox", "Tokens bleiben aus der Sandbox"),
             "captions": [
                 T("Git logins, publish logins and API keys stay in your keyring. The proxy adds them on the wire, only for the server whose certificate it checked.",
                   "Git-Logins, Publish-Logins und API-Schlüssel bleiben in Ihrem Schlüsselbund. Der Proxy setzt sie auf der Leitung ein, nur für den Server mit geprüftem Zertifikat."),
                 T("The agent can clone and push, but never reads a token.",
                   "Der Agent kann klonen und pushen, liest aber nie ein Token."),
             ]},
            {"kind": "terminal", "heading": T("git-logins and publish-logins", "git-logins und publish-logins"),
             "steps": [
                 {"comment": T("the token from standard input, into the keyring", "das Token von der Standardeingabe, in den Schlüsselbund"),
                  "cmd": "sandbox-ai secrets set GITEA_TOKEN", "live": True, "stdin": "gitea_0123456789abcdef0123\n"},
                 {"cmd": "sandbox-ai git-logins add https://git.example.org --secret GITEA_TOKEN --user me", "live": True},
                 {"cmd": "sandbox-ai git-logins list", "live": True},
                 {"cmd": "sandbox-ai publish-logins add pypi --secret PYPI_TOKEN"},
                 {"comment": T("this start may publish; every upload is asked first", "dieser Start darf veröffentlichen; jeder Upload wird vorher erfragt"),
                  "cmd": "sandbox-ai claude --publish pypi"},
             ],
             "captions": [
                 T("List each Git server once; every sandbox's proxy then logs in for the agent. Publishing to PyPI or npm is allowed per start with --publish.",
                   "Jeden Git-Server einmal eintragen; danach meldet der Proxy jeder Sandbox den Agenten an. Veröffentlichen auf PyPI oder npm erlaubt --publish je Start."),
                 T("Several accounts become identities: sandbox-ai identities add, then start with --identity NAME.",
                   "Mehrere Konten werden zu Identitäten: sandbox-ai identities add, dann Start mit --identity NAME."),
             ]},
        ],
    },
    {
        "id": "files",
        "title": T("Files, workspaces and limits", "Dateien, Workspaces und Limits"),
        "topics": [T("Workspaces and mounts", "Workspaces und Mounts"),
                   T("resume NAME --restart --workspace DIR", "resume NAME --restart --workspace DIR"),
                   T("Memory limits and the memory watch", "Speicherlimits und die Speicherüberwachung")],
        "scenes": [
            {"kind": "diagram", "diagram": "mounts", "heading": T("What appears where", "Was wo erscheint"),
             "captions": [
                 T("The first workspace is always /work; further ones appear at /workspaces/NAME. Extra mounts go under /work, /mnt or /opt, read-only or writable.",
                   "Der erste Workspace ist immer /work; weitere erscheinen unter /workspaces/NAME. Zusätzliche Mounts liegen unter /work, /mnt oder /opt, schreibgeschützt oder beschreibbar."),
                 T("Your HOME, ~/.ssh, ~/.config, folders on your PATH and sandbox-ai itself are refused as workspaces.",
                   "Ihr HOME, ~/.ssh, ~/.config, Ordner im PATH und sandbox-ai selbst werden als Workspace abgelehnt."),
             ]},
            {"kind": "terminal", "heading": T("Several workspaces and a mount", "Mehrere Workspaces und ein Mount"),
             "steps": [
                 {"cmd": "sandbox-ai claude -w ~/src/shop,~/src/lib -m ~/data:/mnt/data:ro --dry-run", "live": True,
                  "pick": ["workspaces", "mounts", "workspace_mode"]},
             ],
             "captions": [
                 T("Separate workspaces with commas or repeat -w. --workspace-ro makes every workspace read-only.",
                   "Workspaces mit Kommas trennen oder -w wiederholen. --workspace-ro macht jeden Workspace schreibgeschützt."),
             ]},
            {"kind": "terminal", "heading": T("Add a folder to a start", "Einem Start einen Ordner hinzufügen"),
             "steps": [
                 {"comment": T("stopped and started again with /workspaces/lib, kept for later starts",
                               "beendet und neu gestartet mit /workspaces/lib, für spätere Starts behalten"),
                  "cmd": "sandbox-ai resume shop --restart --workspace ~/src/lib"},
                 {"cmd": "sandbox-ai resume shop --mount ~/data:/mnt/data:ro"},
                 {"comment": T("change the limits while it runs", "die Limits im Lauf ändern"),
                  "cmd": "sandbox-ai limits shop --memory 16g --cpus 4"},
             ],
             "captions": [
                 T("resume NAME --restart --workspace DIR stops a running start and brings it back with the folder added; the agent continues its conversation.",
                   "resume NAME --restart --workspace DIR beendet einen laufenden Start und holt ihn mit dem zusätzlichen Ordner zurück; der Agent setzt sein Gespräch fort."),
             ]},
            {"kind": "diagram", "diagram": "memory", "heading": T("Limits and the memory watch", "Limits und Speicherüberwachung"),
             "captions": [
                 T("Every sandbox has limits on memory, CPUs and processes. Files in /tmp live in memory and count against the limit.",
                   "Jede Sandbox hat Limits für Speicher, CPUs und Prozesse. Dateien in /tmp liegen im Speicher und zählen zum Limit."),
                 T("The launcher watches the memory from the host: at 85 % it warns you and the agent, at 95 % it ends the biggest test or build so that the sandbox survives.",
                   "Der Starter beobachtet den Speicher vom Host aus: bei 85 % warnt er Sie und den Agenten, bei 95 % beendet er den größten Test oder Build, damit die Sandbox überlebt."),
             ]},
        ],
    },
    {
        "id": "hardware",
        "title": T("Devices, GPU, display and sound", "Geräte, GPU, Anzeige und Ton"),
        "topics": [T("Hardware, safest first", "Hardware, das Sicherste zuerst"),
                   T("--gui, VNC and Wayland", "--gui, VNC und Wayland"), T("--audio virtual", "--audio virtual")],
        "scenes": [
            {"kind": "bullets", "heading": T("Hardware for the agent, safest first", "Hardware für den Agenten, das Sicherste zuerst"),
             "bullets": [
                 T("1. A service on the host, over the network: ser2net, openocd, adb, Ollama with `--allow-host-port PORT`",
                   "1. Ein Dienst auf dem Host, übers Netz: ser2net, openocd, adb, Ollama mit `--allow-host-port PORT`"),
                 T("2. The device itself: `--device /dev/ttyUSB0` or `--device usb:0403:6001`",
                   "2. Das Gerät selbst: `--device /dev/ttyUSB0` oder `--device usb:0403:6001`"),
                 T("3. The GPU in one option: `--gpu nvidia`, `--gpu dri`, `--gpu rocm`",
                   "3. Die GPU in einer Option: `--gpu nvidia`, `--gpu dri`, `--gpu rocm`"),
                 T("A device's driver runs in your host's kernel: it weakens isolation",
                   "Der Treiber eines Geräts läuft im Kernel Ihres Hosts: das schwächt die Isolation"),
             ],
             "captions": [
                 T("The safest way is a service on the host that the sandbox reaches over the network: no host driver is exposed.",
                   "Am sichersten ist ein Dienst auf dem Host, den die Sandbox übers Netz erreicht: kein Host-Treiber wird freigelegt."),
                 T("--device and --gpu hand the device itself to the agent; device rules can allow, ask about or deny each one.",
                   "--device und --gpu geben dem Agenten das Gerät selbst; Geräteregeln können jedes erlauben, erfragen oder ablehnen."),
             ]},
            {"kind": "terminal", "heading": T("Devices while it runs, and a device to practise on", "Geräte im Lauf, und ein Übungsgerät"),
             "steps": [
                 {"cmd": "sandbox-ai devices add shop usb:0403:6001"},
                 {"cmd": "sandbox-ai devices list shop"},
                 {"comment": T("an emulated serial device on 127.0.0.1:4000", "ein emuliertes serielles Gerät auf 127.0.0.1:4000"),
                  "cmd": "sandbox-ai demo-device start"},
                 {"cmd": "sandbox-ai claude --preset hardware-analysis"},
             ],
             "captions": [
                 T("Devices can be handed to a running sandbox, and a replugged USB device is attached again.",
                   "Geräte lassen sich einer laufenden Sandbox geben, und ein neu eingestecktes USB-Gerät wird wieder angehängt."),
                 T("The demo device is fixed text: a made-up sensor node with U-Boot and a small shell, ideal for trying the hardware preset.",
                   "Das Demogerät ist fester Text: ein erfundener Sensorknoten mit U-Boot und einer kleinen Shell, ideal zum Ausprobieren des Hardware-Presets."),
             ]},
            {"kind": "terminal", "heading": T("A display and sound inside", "Anzeige und Ton drinnen"),
             "steps": [
                 {"cmd": "sandbox-ai claude --gui --audio virtual --dry-run", "live": True, "pick": ["gui", "audio"]},
                 {"comment": T("watch it over VNC on a Unix socket", "über VNC auf einem Unix-Socket zusehen"),
                  "cmd": "sandbox-ai claude --gui-vnc ~/.local/state/sandbox-ai/vnc"},
             ],
             "captions": [
                 T("--gui starts a virtual X display for GUI programs and headed browsers; --gui-vnc lets you watch it over a Unix socket.",
                   "--gui startet eine virtuelle X-Anzeige für GUI-Programme und sichtbare Browser; --gui-vnc lässt Sie über einen Unix-Socket zusehen."),
                 T("--gui-wayland shows windows on your desktop but weakens isolation. X11 passthrough is deliberately not offered.",
                   "--gui-wayland zeigt Fenster auf Ihrem Desktop, schwächt aber die Isolation. X11-Durchreichung wird bewusst nicht angeboten."),
                 T("--audio virtual gives the sandbox a sound server with a null sink: programs can play and record, nothing reaches your speakers or microphones.",
                   "--audio virtual gibt der Sandbox einen Soundserver mit Null-Senke: Programme können abspielen und aufnehmen, nichts erreicht Lautsprecher oder Mikrofone."),
             ]},
        ],
    },
    {
        "id": "kits",
        "title": T("Kits, skills, plugins, hooks, rulesets", "Kits, Skills, Plugins, Hooks, Regelwerke"),
        "topics": [T("Kits and the built-in skill", "Kits und der eingebaute Skill"),
                   T("Plugins and hooks", "Plugins und Hooks"), T("Rulesets: allow, ask, deny", "Regelwerke: allow, ask, deny")],
        "scenes": [
            {"kind": "bullets", "heading": T("Kits bring the agent's tools along", "Kits bringen die Werkzeuge des Agenten mit"),
             "bullets": [
                 T("A kit: skills, agents, commands, prompts, a memory template, plugins, hooks",
                   "Ein Kit: Skills, Agents, Commands, Prompts, eine Memory-Vorlage, Plugins, Hooks"),
                 T("Default kit `~/.local/share/sandbox-ai/home/AGENT`, in every sandbox",
                   "Standard-Kit `~/.local/share/sandbox-ai/home/AGENT`, in jeder Sandbox"),
                 T("Built-in skill `sandbox-ai`: how to work in the sandbox", "Eingebauter Skill `sandbox-ai`: wie man in der Sandbox arbeitet"),
                 T("`--kit NAME` adds a kit; project memory is kept per project", "`--kit NAME` fügt ein Kit hinzu; Projekt-Memory wird je Projekt aufbewahrt"),
                 T("Plugins: `sandbox-ai plugins claude` fills a store, read-only in every sandbox",
                   "Plugins: `sandbox-ai plugins claude` füllt einen Speicher, schreibgeschützt in jeder Sandbox"),
             ],
             "captions": [
                 T("Kits give every sandbox the agent's skills, agents and commands without exposing your own ~/.claude or ~/.codex.",
                   "Kits geben jeder Sandbox Skills, Agents und Commands des Agenten, ohne Ihr eigenes ~/.claude oder ~/.codex freizulegen."),
                 T("Every sandbox also gets the built-in skill sandbox-ai, which tells the agent what it may reach and which command you need to unblock something.",
                   "Jede Sandbox bekommt auch den eingebauten Skill sandbox-ai, der dem Agenten sagt, was er erreicht und welchen Befehl Sie zum Freischalten brauchen."),
             ]},
            {"kind": "terminal", "heading": T("Managing kits", "Kits verwalten"),
             "steps": [
                 {"cmd": "sandbox-ai kits new review", "live": True},
                 {"cmd": "sandbox-ai kits new-skill review lint-fix --description \"Fixes lint errors; use after edits\"", "live": True},
                 {"cmd": "sandbox-ai kits show review", "live": True, "head": 4},
                 {"cmd": "sandbox-ai claude --kit review"},
             ],
             "captions": [
                 T("sandbox-ai kits creates kits, copies or writes skills and declares plugins, without editing files by hand.",
                   "sandbox-ai kits legt Kits an, kopiert oder schreibt Skills und deklariert Plugins, ohne Dateien von Hand zu bearbeiten."),
             ]},
            {"kind": "bullets", "heading": T("Hooks run only after your review", "Hooks laufen erst nach Ihrer Prüfung"),
             "bullets": [
                 T("`sandbox-ai kits add-hook review ~/hooks/check.sh --matcher Bash`",
                   "`sandbox-ai kits add-hook review ~/hooks/check.sh --matcher Bash`"),
                 T("`sandbox-ai kits trust-hooks review`: read every file, then approve",
                   "`sandbox-ai kits trust-hooks review`: jede Datei lesen, dann freigeben"),
                 T("A changed hook stops running until approved again", "Ein geänderter Hook läuft erst nach neuer Freigabe wieder"),
             ],
             "captions": [
                 T("A kit can carry the agent's hooks. They run only after you reviewed and approved them, and only as long as nothing in them changes.",
                   "Ein Kit kann Hooks des Agenten mitbringen. Sie laufen erst nach Ihrer Prüfung und Freigabe, und nur solange sich nichts an ihnen ändert."),
             ]},
            {"kind": "terminal", "heading": T("Rulesets: allow, ask or deny each request", "Regelwerke: jede Anfrage erlauben, erfragen, ablehnen"),
             "steps": [
                 {"cmd": "cat ~/rules.json", "file": "~/rules.json"},
                 {"cmd": "sandbox-ai claude --allow-github --rules ~/rules.json --dry-run", "live": True,
                  "pick": ["rulesets[].name", "rulesets[].http"]},
             ],
             "captions": [
                 T("--allow-github opens the whole API. A ruleset narrows it: no gists here, and creating a release is asked first.",
                   "--allow-github öffnet die ganze API. Ein Regelwerk verengt sie: hier keine Gists, und ein Release anzulegen wird vorher erfragt."),
                 T("Rules cover HTTP requests, MCP tool calls and devices. They come from the administrator, you and the project; the strictest decision wins.",
                   "Regeln gelten für HTTP-Anfragen, MCP-Tool-Aufrufe und Geräte. Sie kommen vom Administrator, von Ihnen und vom Projekt; die strengste Entscheidung gewinnt."),
             ]},
        ],
    },
    {
        "id": "mcp",
        "title": T("MCP servers", "MCP-Server"),
        "topics": [T("Each server in its own sandbox", "Jeder Server in seiner eigenen Sandbox"),
                   T("Catalog servers", "Server aus Katalogen"), T("Another agent as a helper", "Ein anderer Agent als Helfer")],
        "scenes": [
            {"kind": "diagram", "diagram": "mcp", "heading": T("A server in a sandbox of its own", "Ein Server in einer eigenen Sandbox"),
             "captions": [
                 T("Local MCP servers run in their own sandbox, without credentials and without network unless their definition says otherwise.",
                   "Lokale MCP-Server laufen in einer eigenen Sandbox, ohne Zugangsdaten und ohne Netz, außer ihre Definition sagt es."),
                 T("There is no network path between the sandboxes: the agent talks through a Unix socket and a relay that applies the MCP rules and logs every call.",
                   "Zwischen den Sandboxes gibt es keinen Netzweg: Der Agent spricht über einen Unix-Socket und ein Relay, das die MCP-Regeln anwendet und jeden Aufruf protokolliert."),
             ]},
            {"kind": "terminal", "heading": T("Using MCP servers", "MCP-Server nutzen"),
             "steps": [
                 {"cmd": "sandbox-ai mcp list", "live": True},
                 {"comment": T("the built-in demo server, started if needed", "der eingebaute Demo-Server, bei Bedarf gestartet"),
                  "cmd": "sandbox-ai claude --mcp demo"},
                 {"comment": T("from a catalog, pulled by digest", "aus einem Katalog, per Digest geladen"),
                  "cmd": "sandbox-ai mcp add duckduckgo --network full"},
                 {"cmd": "sandbox-ai mcp stop demo"},
             ],
             "captions": [
                 T("--mcp NAME starts the server's sandbox if needed and connects the agent; the server keeps running for other agents.",
                   "--mcp NAME startet die Sandbox des Servers bei Bedarf und verbindet den Agenten; der Server läuft für andere Agenten weiter."),
                 T("Your own servers go into ~/.config/sandbox-ai/mcp.json; ready-made ones come from audited catalogs, pinned by digest.",
                   "Eigene Server kommen nach ~/.config/sandbox-ai/mcp.json; fertige stammen aus geprüften Katalogen, per Digest festgelegt."),
             ]},
            {"kind": "bullets", "heading": T("Agents helping agents", "Agenten helfen Agenten"),
             "bullets": [
                 T("`--helper codex`: Codex as an MCP server in a sandbox of its own; the agent gives it tasks with its tool `ask`",
                   "`--helper codex`: Codex als MCP-Server in einer eigenen Sandbox; der Agent gibt ihm Aufgaben mit dem Tool `ask`"),
                 T("`--with codex`: Codex in the same sandbox, called from the shell (needs `sandbox-ai build team`)",
                   "`--with codex`: Codex in derselben Sandbox, aus der Shell aufgerufen (braucht `sandbox-ai build team`)"),
                 T("Each helper brings its own login and its provider's hosts", "Jeder Helfer bringt seinen eigenen Login und die Hosts seines Providers mit"),
             ],
             "captions": [
                 T("A second agent can help as an MCP server in its own sandbox, or live in the same sandbox for the first one to call.",
                   "Ein zweiter Agent kann als MCP-Server in einer eigenen Sandbox helfen oder in derselben Sandbox wohnen, damit der erste ihn aufruft."),
             ]},
        ],
    },
    {
        "id": "llm-vpn",
        "title": T("LLM servers, VPN and Tor", "LLM-Server, VPN und Tor"),
        "topics": [T("Local and remote models with --llm", "Lokale und entfernte Modelle mit --llm"),
                   T("VPN profiles with a kill switch", "VPN-Profile mit Kill-Switch"), T("Tor", "Tor")],
        "scenes": [
            {"kind": "terminal", "heading": T("Your own model server", "Ihr eigener Modellserver"),
             "steps": [
                 {"cmd": "sandbox-ai llm add gpu --kind ollama --url http://192.168.1.20:11434", "live": True},
                 {"cmd": "sandbox-ai llm check gpu --agent claude"},
                 {"cmd": "sandbox-ai claude --llm gpu:qwen2.5-coder:14b --no-provider-defaults --dry-run", "live": True,
                  "pick": ["llm.name", "llm.api", "llm.opens", "llm.placeholder", "https_hosts", "allow_tcp"]},
             ],
             "captions": [
                 T("Ollama, LM Studio, llama.cpp, vLLM or any OpenAI-compatible server, local or remote: register it once by name.",
                   "Ollama, LM Studio, llama.cpp, vLLM oder jeder OpenAI-kompatible Server, lokal oder entfernt: einmal mit Namen anlegen."),
                 T("--llm opens exactly that address and port and configures the agent; with --no-provider-defaults it reaches nothing else. Its key stays with the proxy.",
                   "--llm öffnet genau diese Adresse und diesen Port und richtet den Agenten ein; mit --no-provider-defaults erreicht er nichts sonst. Der Schlüssel bleibt beim Proxy."),
             ]},
            {"kind": "diagram", "diagram": "vpn", "heading": T("A VPN in front of the sandbox", "Ein VPN vor der Sandbox"),
             "captions": [
                 T("With the Podman backend a VPN container joins the pod: everything the sandbox sends leaves through the tunnel, and a kill switch blocks the rest.",
                   "Mit dem Podman-Backend tritt ein VPN-Container dem Pod bei: Alles, was die Sandbox sendet, geht durch den Tunnel, ein Kill-Switch blockiert den Rest."),
                 T("Before the agent starts, the addresses and DNS seen from outside are checked; a failed check stops the start.",
                   "Vor dem Agentstart werden die von außen sichtbaren Adressen und DNS geprüft; eine fehlgeschlagene Prüfung stoppt den Start."),
             ]},
            {"kind": "terminal", "heading": T("VPN and Tor profiles", "VPN- und Tor-Profile"),
             "steps": [
                 {"comment": T("once: the VPN container image", "einmal: das Image des VPN-Containers"), "cmd": "sandbox-ai build vpn"},
                 {"cmd": "sandbox-ai vpn add home --wireguard ~/Downloads/wg-home.conf"},
                 {"cmd": "sandbox-ai vpn add mv --mullvad --location se-sto"},
                 {"cmd": "sandbox-ai vpn add anon --tor --socks --exit-country de"},
                 {"cmd": "sandbox-ai vpn check mv"},
                 {"cmd": "sandbox-ai claude --vpn anon"},
             ],
             "captions": [
                 T("WireGuard, OpenVPN, Mullvad, Tailscale and Tor profiles; their keys stay in sandbox-ai secrets.",
                   "Profile für WireGuard, OpenVPN, Mullvad, Tailscale und Tor; ihre Schlüssel bleiben in sandbox-ai secrets."),
                 T("With a Tor profile every TCP connection and every lookup goes through Tor, and IPv6 and other UDP are dropped.",
                   "Mit einem Tor-Profil läuft jede TCP-Verbindung und jede Namensabfrage über Tor, IPv6 und anderes UDP werden verworfen."),
             ]},
        ],
    },
    {
        "id": "presets-wizard",
        "title": T("Presets and the wizard", "Presets und der Wizard"),
        "topics": [T("Ready setups: --preset NAME", "Fertige Setups: --preset NAME"),
                   T("The project file and its trust", "Die Projektdatei und ihr Vertrauen"), T("Your defaults", "Ihre Vorgaben")],
        "scenes": [
            {"kind": "terminal", "heading": T("Presets: ready setups", "Presets: fertige Setups"),
             "steps": [
                 {"cmd": "sandbox-ai presets list", "live": True, "head": 18},
                 {"cmd": "sandbox-ai presets show software-analysis", "live": True},
             ],
             "captions": [
                 T("A preset is launcher options, MCP servers, a ruleset and a first task, with placeholders for what only you know.",
                   "Ein Preset sind Starter-Optionen, MCP-Server, ein Regelwerk und eine erste Aufgabe, mit Platzhaltern für das, was nur Sie wissen."),
                 T("Analysis, development, testing and fun: the command line wins over a preset, and a preset's mode is only a floor.",
                   "Analyse, Entwicklung, Tests und Spaß: Die Kommandozeile gewinnt über ein Preset, und dessen Modus ist nur eine Untergrenze."),
             ]},
            {"kind": "terminal", "heading": T("The wizard writes the project file", "Der Wizard schreibt die Projektdatei"),
             "steps": [
                 {"cmd": "sandbox-ai wizard", "example": True, "out": [
                     "Project directory (mounted at /work) [/home/me/src/shop]:",
                     "Security mode (lax/low/medium/high/paranoid) [medium]:",
                     "Agent (codex/claude/opencode): claude",
                     "Network (default/development/frontier/dns/egress-all/offline) [default]: development",
                     "Start the agent without permission prompts (--dangerously-skip-permissions) [y/N]: y",
                     "Log denied egress and file access (--log-dir) [y/N]: y",
                     "Write configuration [Y/n]:",
                     "Wrote and trusted /home/me/src/shop/.sandbox-ai.json",
                 ]},
                 {"cmd": "sandbox-ai wizard edit ~/src/shop"},
                 {"comment": T("a changed or cloned file is applied only once you trust it", "eine geänderte oder geklonte Datei gilt erst, wenn Sie ihr vertrauen"),
                  "cmd": "sandbox-ai wizard trust ~/src/shop/.sandbox-ai.json"},
             ],
             "captions": [
                 T("The wizard asks for the security mode first and then offers only what the mode allows. It writes .sandbox-ai.json and trusts it.",
                   "Der Wizard fragt zuerst nach dem Sicherheitsmodus und bietet dann nur an, was der Modus erlaubt. Er schreibt .sandbox-ai.json und vertraut ihr."),
                 T("The agent can write the workspace, so a changed project file is never applied until you review and trust it again.",
                   "Der Agent kann den Workspace beschreiben, darum gilt eine geänderte Projektdatei erst wieder, wenn Sie sie geprüft und ihr vertraut haben."),
             ]},
            {"kind": "terminal", "heading": T("Your defaults for every start", "Ihre Vorgaben für jeden Start"),
             "steps": [
                 {"cmd": "sandbox-ai defaults set mode high", "live": True},
                 {"cmd": "sandbox-ai defaults show", "live": True},
                 {"cmd": "sandbox-ai defaults unset mode", "live": True},
             ],
             "captions": [
                 T("defaults.json holds your choices for every start: the security mode, canaries, telemetry, expiry of kept starts and the sandbox subnet.",
                   "defaults.json enthält Ihre Wahl für jeden Start: Sicherheitsmodus, Köder, Telemetrie, Ablauf aufbewahrter Starts und das Sandbox-Subnetz."),
             ]},
        ],
    },
    {
        "id": "app-web",
        "title": T("The desktop app and the web interface", "Die Desktop-App und die Weboberfläche"),
        "topics": [T("Pages, board, terminals, approvals", "Seiten, Tafel, Terminals, Freigaben"),
                   T("sandbox-ai web", "sandbox-ai web"), T("Other hosts over SSH", "Andere Hosts über SSH")],
        "scenes": [
            {"kind": "app", "page": "launch", "heading": T("The app: Start", "Die App: Start"),
             "bullets": [
                 T("Every launcher option, grouped in tabs", "Jede Starter-Option, in Reitern gruppiert"),
                 T("The resulting command line, live", "Die entstehende Kommandozeile, live"),
                 T("Dry run, start in the app, in a window or as a session", "Probelauf, Start in der App, im Fenster oder als Session"),
             ],
             "captions": [
                 T("sandbox-ai-app is a graphical front end for everything shown so far. Every action runs the sandbox-ai command itself.",
                   "sandbox-ai-app ist eine grafische Oberfläche für alles bisher Gezeigte. Jede Aktion führt den sandbox-ai-Befehl selbst aus."),
                 T("The Start page is built from the launcher's own option parser, so a new option appears without a new app.",
                   "Die Seite Start wird aus dem Optionsparser des Starters gebaut, eine neue Option erscheint also ohne neue App."),
             ]},
            {"kind": "app", "page": "board", "heading": T("The board: one sandbox as a graph", "Die Tafel: eine Sandbox als Graph"),
             "bullets": [
                 T("Agent, workspaces, proxy, network, MCP, VPN, limits as nodes", "Agent, Workspaces, Proxy, Netz, MCP, VPN, Limits als Knoten"),
                 T("A security score and the launcher's own verdict", "Ein Sicherheitswert und das Urteil des Starters"),
                 T("New from preset, Wizard, Apply writes `.sandbox-ai.json`", "Neu aus Vorlage, Assistent; Übernehmen schreibt `.sandbox-ai.json`"),
             ],
             "captions": [
                 T("The board shows one sandbox as a node graph. Problems, badges and a security score follow every change.",
                   "Die Tafel zeigt eine Sandbox als Knotengraph. Probleme, Abzeichen und ein Sicherheitswert folgen jeder Änderung."),
                 T("Apply shows the change, writes the project file and trusts it.",
                   "Übernehmen zeigt die Änderung, schreibt die Projektdatei und vertraut ihr."),
             ]},
            {"kind": "app", "page": "sandboxes", "heading": T("Sandboxes, sessions and terminals", "Sandboxen, Sitzungen und Terminals"),
             "bullets": [
                 T("Every instance of this account in one table", "Jede Instanz dieses Kontos in einer Tabelle"),
                 T("Attach, pause, resume, stop; allow, revoke; devices and limits", "Verbinden, pausieren, fortsetzen, beenden; erlauben, widerrufen; Geräte und Limits"),
                 T("Terminal tabs inside the app (xterm.js)", "Terminal-Reiter in der App (xterm.js)"),
             ],
             "captions": [
                 T("Sandboxes and sessions lists everything that runs or is kept, wherever it was started, with buttons for each command.",
                   "Sandboxen & Sitzungen listet alles, was läuft oder aufbewahrt ist, egal wo gestartet, mit Knöpfen für jeden Befehl."),
                 T("Sessions can be attached in a terminal tab of the app or in a terminal window.",
                   "Sessions lassen sich in einem Terminal-Reiter der App oder in einem Terminalfenster verbinden."),
             ]},
            {"kind": "app", "page": "approvals", "heading": T("Approvals in the app", "Rückfragen in der App: Freigaben"),
             "bullets": [
                 T("Open questions of all sandboxes, with a countdown", "Offene Rückfragen aller Sandboxes, mit Countdown"),
                 T("Allow with the scope once, run or always; deny; block always", "Erlauben mit Reichweite once, run oder always; ablehnen; immer blockieren"),
                 T("Desktop notification and a count in the navigation", "Desktop-Benachrichtigung und ein Zähler in der Navigation"),
             ],
             "captions": [
                 T("The approvals page shows the open questions of every running sandbox, with the run, what is asked and the countdown to the refusal.",
                   "Die Seite Freigaben zeigt die offenen Fragen jeder laufenden Sandbox, mit Lauf, Anliegen und Countdown bis zur Ablehnung."),
                 T("It keeps answering from the system tray while the window is closed.",
                   "Sie antwortet weiter aus dem Infobereich, während das Fenster geschlossen ist."),
             ]},
            {"kind": "terminal", "heading": T("The web interface", "Die Weboberfläche"),
             "steps": [
                 {"comment": T("a password (12+ characters) and a TOTP app", "ein Passwort (12+ Zeichen) und eine TOTP-App"),
                  "cmd": "sandbox-ai web user add admin"},
                 {"comment": T("xterm.js for the terminals, pinned and checked", "xterm.js für die Terminals, festgelegt und geprüft"),
                  "cmd": "sandbox-ai web vendor"},
                 {"cmd": "sandbox-ai web serve"},
                 {"cmd": "sandbox-ai web serve --listen 0.0.0.0:8443 --hostname box.lan"},
             ],
             "captions": [
                 T("sandbox-ai web is the app's counterpart in the browser: HTTPS only, password and one-time code, an audit log.",
                   "sandbox-ai web ist das Gegenstück der App im Browser: nur HTTPS, Passwort und Einmalcode, ein Audit-Log."),
                 T("Each action is one sandbox-ai command with every argument quoted: the browser cannot send a shell command.",
                   "Jede Aktion ist ein sandbox-ai-Befehl mit einzeln gequoteten Argumenten: Der Browser kann keinen Shell-Befehl senden."),
             ]},
            {"kind": "diagram", "diagram": "web", "heading": T("One place for every host", "Ein Ort für jeden Host"),
             "captions": [
                 T("The app and the web interface manage this machine and every host of sandbox-ai hosts, such as Proxmox LXC containers, over SSH.",
                   "App und Weboberfläche verwalten diesen Rechner und jeden Host aus sandbox-ai hosts, etwa Proxmox-LXC-Container, über SSH."),
             ]},
            {"kind": "terminal", "heading": T("Other hosts", "Andere Hosts"),
             "steps": [
                 {"cmd": "sandbox-ai hosts add ct1 user@ct1.lan --description \"Proxmox 101\"", "live": True},
                 {"cmd": "sandbox-ai hosts list", "live": True},
                 {"cmd": "sandbox-ai hosts test ct1"},
                 {"cmd": "sandbox-ai hosts run ct1 -- list"},
             ],
             "captions": [
                 T("Hosts are reached with your SSH keys; the host key is learnt at the first contact and checked strictly after that.",
                   "Hosts werden mit Ihren SSH-Schlüsseln erreicht; der Host-Schlüssel wird beim ersten Kontakt gelernt und danach streng geprüft."),
             ]},
        ],
    },
    {
        "id": "teams",
        "title": T("Agent teams", "Agenten-Teams"),
        "topics": [T("One sandbox per agent", "Eine Sandbox je Agent"), T("The task board", "Das Aufgabenbrett"),
                   T("Limits of teams", "Grenzen von Teams")],
        "scenes": [
            {"kind": "diagram", "diagram": "team", "heading": T("A team works through a task board", "Ein Team arbeitet über ein Aufgabenbrett"),
             "captions": [
                 T("sandbox-ai team starts one sandbox per agent on a shared workspace. The first agent leads and splits the work.",
                   "sandbox-ai team startet eine Sandbox je Agent auf einem gemeinsamen Workspace. Der erste Agent leitet und teilt die Arbeit auf."),
                 T("The task board is an MCP server in a sandbox of its own, without network; every member reaches it through its relay, so the MCP rules hold.",
                   "Das Aufgabenbrett ist ein MCP-Server in einer eigenen Sandbox, ohne Netz; jedes Mitglied erreicht es über sein Relay, die MCP-Regeln gelten also."),
             ]},
            {"kind": "terminal", "heading": T("Running a team", "Ein Team betreiben"),
             "steps": [
                 {"cmd": "sandbox-ai team start app --agents claude,codex,opencode -w ~/src/app -- --allow-pypi"},
                 {"comment": T("talk to the lead", "mit der Leitung sprechen"), "cmd": "sandbox-ai attach app-claude"},
                 {"cmd": "sandbox-ai team show app"},
                 {"cmd": "sandbox-ai team post app codex \"run the tests\""},
                 {"cmd": "sandbox-ai team stop app"},
                 {"cmd": "sandbox-ai team delete app --yes"},
             ],
             "captions": [
                 T("Each member is a named session such as app-claude: attach, pause or stop it like any start. Options after -- hold for every member.",
                   "Jedes Mitglied ist eine benannte Session wie app-claude: verbinden, pausieren oder beenden wie jeden Start. Optionen nach -- gelten für alle."),
                 T("You can post tasks yourself; team show prints the members and the board.",
                   "Sie können selbst Aufgaben einstellen; team show zeigt die Mitglieder und das Brett."),
             ]},
            {"kind": "bullets", "heading": T("Good to know", "Gut zu wissen"),
             "bullets": [
                 T("Members share the workspace: nothing stops two from editing one file",
                   "Mitglieder teilen den Workspace: nichts hindert zwei daran, eine Datei zu ändern"),
                 T("The board does not know who calls it: a member names itself", "Das Brett weiß nicht, wer es aufruft: ein Mitglied nennt sich selbst"),
                 T("A task taken but never reported comes back at the next wait", "Eine genommene, nie gemeldete Aufgabe kommt beim nächsten Warten wieder"),
             ],
             "captions": [
                 T("The lead's prompt asks it to split the work, but the members are your own agents in one workspace: plan accordingly.",
                   "Der Prompt der Leitung bittet sie, die Arbeit aufzuteilen, aber die Mitglieder sind Ihre Agenten in einem Workspace: planen Sie entsprechend."),
             ]},
        ],
    },
    {
        "id": "logs",
        "title": T("Logs, activity and troubleshooting", "Logs, Aktivität und Fehlersuche"),
        "topics": [T("What was refused, and how to allow it", "Was abgelehnt wurde, und wie man es erlaubt"),
                   T("The agents' timeline", "Die Zeitleiste der Agenten"), T("When something fails", "Wenn etwas scheitert")],
        "scenes": [
            {"kind": "bullets", "heading": T("Logs outside the agent's reach", "Logs außerhalb der Reichweite des Agenten"),
             "bullets": [
                 T("`connections-*.jsonl`: every request, push and refusal, credentials redacted",
                   "`connections-*.jsonl`: jede Anfrage, jeder Push, jede Ablehnung, Zugangsdaten geschwärzt"),
                 T("`egress-denied.jsonl`, `files.jsonl`, `processes.jsonl` with `--log-dir DIR`",
                   "`egress-denied.jsonl`, `files.jsonl`, `processes.jsonl` mit `--log-dir DIR`"),
                 T("`sandbox-ai logs`: blocked targets with the option that allows each",
                   "`sandbox-ai logs`: blockierte Ziele mit der Option, die jedes erlaubt"),
                 T("The app: Connections, Activity, Statistics", "Die App: Verbindungen, Aktivität, Statistik"),
             ],
             "captions": [
                 T("Every run writes a connection log; with --log-dir you also get refused destinations, file access and processes. The agent cannot see these files.",
                   "Jeder Lauf schreibt ein Verbindungsprotokoll; mit --log-dir kommen abgelehnte Ziele, Dateizugriffe und Prozesse dazu. Der Agent sieht diese Dateien nicht."),
                 T("sandbox-ai logs summarises them and names the option that would allow each blocked target.",
                   "sandbox-ai logs fasst sie zusammen und nennt die Option, die jedes blockierte Ziel erlauben würde."),
             ]},
            {"kind": "terminal", "heading": T("What the agents did", "Was die Agenten taten"),
             "steps": [
                 {"cmd": "sandbox-ai logs --project ~/src/shop"},
                 {"cmd": "sandbox-ai activity --session shop --follow"},
                 {"cmd": "sandbox-ai activity --kind network,approval --since 2h"},
                 {"cmd": "sandbox-ai activity --runs"},
             ],
             "captions": [
                 T("sandbox-ai activity shows one timeline: prompts, replies, tool calls, commands, file changes, network and approvals, and the tokens of each turn.",
                   "sandbox-ai activity zeigt eine Zeitleiste: Prompts, Antworten, Tool-Aufrufe, Befehle, Dateiänderungen, Netz und Rückfragen, dazu die Tokens je Runde."),
             ]},
            {"kind": "table", "heading": T("Troubleshooting", "Fehlersuche"),
             "columns": [T("Symptom", "Symptom"), T("Check", "Prüfen")],
             "rows": [
                 [T("anything at all", "irgendetwas"), "`sandbox-ai doctor`"],
                 [T("a host or download is blocked", "ein Host oder Download ist blockiert"),
                  T("`sandbox-ai logs`, then `sandbox-ai net allow` or a start option", "`sandbox-ai logs`, dann `sandbox-ai net allow` oder eine Startoption")],
                 [T("Claude: not logged in, login expired", "Claude: nicht angemeldet, Login abgelaufen"),
                  "`sandbox-ai login --status`, `sandbox-ai login claude`"],
                 [T("project configuration is not trusted", "Projektkonfiguration nicht vertraut"),
                  T("review it, then `sandbox-ai wizard trust FILE`", "prüfen, dann `sandbox-ai wizard trust DATEI`")],
                 [T("LLM server unreachable", "LLM-Server nicht erreichbar"), "`sandbox-ai llm check NAME --agent AGENT`"],
                 [T("MCP server fails to start", "MCP-Server startet nicht"),
                  T("`sandbox-ai mcp list`, its supervisor.log", "`sandbox-ai mcp list`, dessen supervisor.log")],
             ],
             "captions": [
                 T("Start with sandbox-ai doctor: it checks every requirement and prints the command that fixes each problem.",
                   "Beginnen Sie mit sandbox-ai doctor: Er prüft jede Voraussetzung und nennt den Befehl, der jedes Problem behebt."),
                 T("Blocked? Look at the logs, then widen exactly what is needed, while the sandbox runs or at the next start.",
                   "Blockiert? Schauen Sie in die Logs und erweitern Sie dann genau das Nötige, im Lauf oder beim nächsten Start."),
             ]},
        ],
    },
]

# The chapters the course must cover (checked by the test), in order.
REQUIRED_CHAPTERS = ["what-why", "install", "first-start", "network", "security", "files", "hardware", "kits",
                     "mcp", "llm-vpn", "presets-wizard", "app-web", "teams", "logs"]

# Scenes saved as stills with --stills: "CHAPTER-ID/INDEX" (0-based scene index), or "intro", "contents", "outro".
STILLS = ["what-why/1", "first-start/0", "network/4", "security/1", "app-web/1", "teams/0"]


def text(value, lang):
    """VALUE in LANG: a T() dictionary, or a plain string shown in every language."""
    return value[lang] if isinstance(value, dict) else value


def scene_id(chapter, index):
    return f"{chapter['id']}/{index}"


def iter_scenes():
    """(chapter, index, scene) for every scene of every chapter."""
    for chapter in CHAPTERS:
        for index, scene in enumerate(chapter["scenes"]):
            yield chapter, index, scene


def texts_of(scene):
    """Every text of SCENE (T() dictionaries and plain strings), for checks."""
    found = [scene.get("heading"), scene.get("note"), *scene.get("captions", []), *scene.get("bullets", [])]
    for row in scene.get("rows", []):
        found += row
    found += scene.get("columns", [])
    for side in ("left", "right"):
        if side in scene:
            found += [scene[side]["title"], *scene[side]["items"]]
    for step in scene.get("steps", []):
        found.append(step.get("comment"))
    if scene.get("kind") == "diagram":
        diagram = DIAGRAMS[scene["diagram"]]
        for box in diagram["boxes"]:
            found += [box.get("label"), box.get("sub")]
        found += [arrow.get("label") for arrow in diagram["arrows"]]
    return [value for value in found if value is not None]


def commands():
    """Every command line the video shows: the terminal steps, and `sandbox-ai ...` in backticks in any text."""
    import re
    found = []
    for chapter, index, scene in iter_scenes():
        where = scene_id(chapter, index)
        for step in scene.get("steps", []):
            found.append((where, step["cmd"]))
        for value in texts_of(scene):
            for lang in LANGUAGES:
                for span in re.findall(r"`([^`]+)`", text(value, lang)):
                    if span.startswith("sandbox-ai "):
                        found.append((where, span))
    for value in COURSE["outro"]["bullets"]:
        for span in re.findall(r"`([^`]+)`", value["en"]):
            if span.startswith("sandbox-ai "):
                found.append(("outro", span))
    return found
