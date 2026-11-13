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
SERIES      series of short videos (pages: one per page of the desktop app), rendered with --series; see there.

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
APP_NAV = [  # the app's own words (its German translation calls approvals "Freigaben")
    (T("Project", "Projekt"), [("launch", T("Start", "Start")), ("board", T("Board", "Board"))]),
    (T("Running", "Laufend"), [("sandboxes", T("Sandboxes & sessions", "Sandboxen & Sitzungen")),
                               ("approvals", T("Approvals", "Freigaben")),
                               ("terminals", T("Terminals", "Terminals"))]),
    (T("Manage", "Verwalten"), [("files", T("File manager", "Dateimanager")), ("kits", T("Kits & plugins", "Kits & Plugins")),
                                ("mcp", T("MCP servers", "MCP-Server")), ("llm", T("LLM servers", "LLM-Server")),
                                ("images", T("Images", "Images")), ("credentials", T("Credentials", "Zugangsdaten")),
                                ("team", T("Teams", "Teams"))]),
    (T("Insight", "Einblick"), [("connections", T("Connections", "Verbindungen")),
                                ("activity", T("Activity", "Aktivität")),
                                ("statistics", T("Statistics", "Statistik"))]),
    (T("System", "System"), [("system", T("System", "System")), ("hosts", T("Hosts", "Hosts")),
                             ("help", T("Help", "Hilfe"))]),
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
             "label": T("Task board: MCP server team-app", "Aufgaben-Board: MCP-Server team-app"),
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
                     "ok    sandbox-ai                0.1.83, the user's own installation "
                     "(/home/me/.local/share/sandbox-ai/versions/0.1.83); Python 3.12.3 (/usr/bin/python3)",
                     "ok    Ordinary user             me",
                     "ok    Python                    3.12.3",
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
                 T("doctor first names its own version and where it is installed, then checks every requirement: rootless Podman, pasta, user namespaces, the proxy's kernel modules, images and logins.",
                   "doctor nennt zuerst seine eigene Version und wo es installiert ist, dann prüft er jede Voraussetzung: rootless Podman, pasta, User-Namespaces, die Kernelmodule des Proxys, Images und Logins."),
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
                     "agent-sandbox: /work is /home/me/src/shop; configuration: none",
                     "agent-sandbox: auth copied: /home/me/.claude/.credentials.json -> /root/.claude/.credentials.json",
                     "agent-sandbox: Claude access token valid for 7.4 h",
                     "agent-sandbox: brisk-otter-lantern is running. Detach: Ctrl-b d (or close the terminal). "
                     "Re-attach: sandbox-ai attach brisk-otter-lantern",
                 ]},
                 {"comment": T("inside: window 'agent' runs Claude, window 'shell' is bash (Ctrl-b n)",
                               "darin: Fenster 'agent' mit Claude, Fenster 'shell' mit bash (Ctrl-b n)"),
                  "cmd": "sandbox-ai attach brisk-otter-lantern"},
             ],
             "captions": [
                 T("Inside, the project is /work: the folder -w names, or without -w the one you start in. An interactive start runs in tmux: the window agent and a shell window in the same sandbox.",
                   "Drinnen ist das Projekt /work: der Ordner, den -w nennt, oder ohne -w der, in dem Sie starten. Ein interaktiver Start läuft in tmux: das Fenster agent und ein Shell-Fenster in derselben Sandbox."),
                 T("Ctrl-b d or closing the terminal detaches; the agent works on, and attach brings you back.",
                   "Ctrl-b d oder das Schließen des Terminals löst; der Agent arbeitet weiter, und attach holt Sie zurück."),
             ]},
            {"kind": "terminal", "heading": T("Every start has a name", "Jeder Start hat einen Namen"),
             "steps": [
                 {"cmd": "sandbox-ai list", "example": True, "out": [
                     "NAME                 KIND     AGENT   STATE    HOME  STARTED (UTC)     WORKSPACE",
                     "brisk-otter-lantern  session  claude  running  kept  2026-11-07 09:12  /home/me/src/shop",
                 ]},
                 {"cmd": "sandbox-ai rename brisk-otter-lantern shop"},
                 {"cmd": "sandbox-ai pause shop"},
                 {"cmd": "sandbox-ai resume shop"},
                 {"cmd": "sandbox-ai stop shop"},
                 {"comment": T("later: reborn, the agent continues its conversation",
                               "später: wiedergeboren, der Agent setzt das Gespräch fort"),
                  "cmd": "sandbox-ai resume shop"},
                 {"cmd": "sandbox-ai list --history shop"},
             ],
             "captions": [
                 T("A start without --name gets a codename such as brisk-otter-lantern; rename gives it another. Every command takes the name.",
                   "Ein Start ohne --name bekommt einen Codenamen wie brisk-otter-lantern; rename gibt ihm einen anderen. Jeder Befehl nimmt den Namen."),
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
                     "ID               SANDBOX                                  QUESTION",
                     "1762506000-41-1  claude-3f9a2b1c (~/src/shop, name shop)  connect to 203.0.113.9:443 (tls), named "
                     "api.example.com by the agent (not verified); asked by curl (pid 7, /usr/bin/curl)",
                     "answer: sandbox-ai approvals allow|deny ID [--scope once|run|always]",
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
                 T("The first workspace, or the directory you start in, is /work; further ones appear at /workspaces/NAME. Extra mounts go under /work, /mnt or /opt, read-only or writable.",
                   "Der erste Workspace, oder das Verzeichnis, in dem Sie starten, ist /work; weitere erscheinen unter /workspaces/NAME. Zusätzliche Mounts liegen unter /work, /mnt oder /opt, schreibgeschützt oder beschreibbar."),
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
                 {"comment": T("while it runs, without a restart: at /workspaces/lib",
                               "im Lauf, ohne Neustart: unter /workspaces/lib"),
                  "cmd": "sandbox-ai workspace add shop ~/src/lib --name lib"},
                 {"comment": T("stopped and started again with /workspaces/lib, kept for later starts",
                               "beendet und neu gestartet mit /workspaces/lib, für spätere Starts behalten"),
                  "cmd": "sandbox-ai resume shop --restart --workspace ~/src/lib"},
                 {"cmd": "sandbox-ai resume shop --mount ~/data:/mnt/data:ro"},
                 {"comment": T("change the limits while it runs", "die Limits im Lauf ändern"),
                  "cmd": "sandbox-ai limits shop --memory 16g --cpus 4"},
             ],
             "captions": [
                 T("workspace add mounts a folder or a file into a running start, with the checks of a start; for a stopped one it waits for the next start.",
                   "workspace add hängt einen Ordner oder eine Datei in einen laufenden Start ein, mit den Prüfungen eines Starts; bei einem beendeten wartet es auf den nächsten Start."),
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
                 {"comment": T("every marketplace and its plugins: sandbox-ai's, the kits', Claude's, Codex's",
                               "jeder Marktplatz und seine Plugins: die von sandbox-ai, der Kits, von Claude, von Codex"),
                  "cmd": "sandbox-ai plugins marketplaces"},
             ],
             "captions": [
                 T("sandbox-ai kits creates kits, copies or writes skills and declares plugins, without editing files by hand; plugins marketplaces shows what each marketplace offers.",
                   "sandbox-ai kits legt Kits an, kopiert oder schreibt Skills und deklariert Plugins, ohne Dateien von Hand zu bearbeiten; plugins marketplaces zeigt, was jeder Marktplatz anbietet."),
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
                 {"comment": T("into a running start, without a restart", "in einen laufenden Start, ohne Neustart"),
                  "cmd": "sandbox-ai mcp attach demo shop --live"},
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
                 {"comment": T("what it has loaded, with sizes and memory", "was er geladen hat, mit Größen und Speicher"),
                  "cmd": "sandbox-ai llm status gpu"},
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
                 {"comment": T("once, for Tor profiles: the Tor container image (Tor is no VPN)", "einmal, für Tor-Profile: das Image des Tor-Containers (Tor ist kein VPN)"), "cmd": "sandbox-ai build tor"},
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
        "topics": [T("Pages, board, terminals, approvals", "Seiten, Board, Terminals, Freigaben"),
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
            {"kind": "app", "page": "board", "shot": "board-preset", "heading": T("The board: one sandbox as a graph", "Das Board: eine Sandbox als Graph"),
             "bullets": [
                 T("Agent, workspaces, proxy, network, MCP, VPN, services, limits as parts, dragged from the list beside it",
                   "Agent, Workspaces, Proxy, Netz, MCP, VPN, Services, Limits als Teile, aus der Liste daneben gezogen"),
                 T("Findings, badges and a security score follow every change", "Befunde, Abzeichen und ein Sicherheitswert folgen jeder Änderung"),
                 T("New from preset… prepares what the preset needs: an identity, an image variant, a Tor profile, MCP servers",
                   "Neu aus Vorlage… bereitet vor, was das Preset braucht: Identität, Image-Variante, Tor-Profil, MCP-Server"),
                 T("Apply… writes `.sandbox-ai.json`; Clone makes a new project or session of it", "Übernehmen… schreibt `.sandbox-ai.json`; Klonen macht ein neues Projekt oder eine Session daraus"),
             ],
             "captions": [
                 T("The board shows one sandbox as a node graph. Problems, badges and a security score follow every change.",
                   "Das Board zeigt eine Sandbox als Knotengraph. Probleme, Abzeichen und ein Sicherheitswert folgen jeder Änderung."),
                 T("A preset is prepared before the start: what it needs is made, then the start goes on. Apply shows the change, writes the project file and trusts it.",
                   "Ein Preset wird vor dem Start vorbereitet: Was es braucht, wird angelegt, dann geht der Start weiter. Übernehmen zeigt die Änderung, schreibt die Projektdatei und vertraut ihr."),
             ]},
            {"kind": "app", "page": "sandboxes", "shot": "sandboxes-cut", "heading": T("Sandboxes, sessions and the kill switch", "Sandboxen, Sessions und der Kill-Switch"),
             "bullets": [
                 T("Every start of this account, grouped with its MCP servers, helpers and team", "Jeder Start dieses Kontos, gruppiert mit MCP-Servern, Helfern und Team"),
                 T("Attach, pause, resume, stop; allow, revoke; devices, limits, workspaces", "Verbinden, pausieren, fortsetzen, beenden; erlauben, zurücknehmen; Geräte, Limits, Workspaces"),
                 T("Kill switch: cut the internet, the local network, all but the model API, or everything", "Kill-Switch: Internet, lokales Netz, alles außer der Modell-API oder alles kappen"),
                 T("Emergency stop (Ctrl+Shift+X): every sandbox here and on your hosts", "Notaus (Strg+Umschalt+X): jede Sandbox hier und auf Ihren Hosts"),
             ],
             "captions": [
                 T("Sandboxes and sessions lists everything that runs or is kept, each start with its parts, and a button for each command.",
                   "Sandboxen & Sitzungen listet alles, was läuft oder aufbewahrt ist, jeden Start mit seinen Teilen, und einen Knopf für jeden Befehl."),
                 T("The kill switch cuts a sandbox's network at once, out of the agent's reach; the emergency stop cuts all of them.",
                   "Der Kill-Switch kappt das Netz einer Sandbox sofort, außerhalb der Reichweite des Agenten; der Notaus kappt alle."),
             ]},
            {"kind": "app", "page": "approvals", "heading": T("Approvals in the app", "Rückfragen in der App: Freigaben"),
             "bullets": [
                 T("Open questions of all sandboxes, each saying why it is asked", "Offene Fragen aller Sandboxen, jede mit dem Grund"),
                 T("Allow once, for the run or always; deny; block always", "Einmal, für den Lauf oder immer erlauben; ablehnen; immer sperren"),
                 T("Blocked: every refusal of the run with its reason and process, to allow or ask again", "Blockiert: jede Ablehnung des Laufs mit Grund und Prozess, zum Erlauben oder Wieder-Fragen"),
             ],
             "captions": [
                 T("The approvals page shows the open questions of every running sandbox, with the run, what is asked and why.",
                   "Die Seite Freigaben zeigt die offenen Fragen jeder laufenden Sandbox, mit Lauf, Anliegen und Grund."),
                 T("Below them, Blocked lists every refusal of the run: one click allows it for the run, or asks again next time.",
                   "Darunter listet Blockiert jede Ablehnung des Laufs: Ein Klick erlaubt sie für den Lauf oder fragt beim nächsten Mal wieder."),
             ]},
            {"kind": "app", "page": "files", "shot": "files-editor", "heading": T("Files: a two-pane file manager", "Dateien: ein Dateimanager mit zwei Bereichen"),
             "bullets": [
                 T("A sandbox's workspaces and HOME beside this machine", "Workspaces und HOME einer Sandbox neben diesem Rechner"),
                 T("Copy, move, drag and drop; nothing gets a mount", "Kopieren, verschieben, Drag and Drop; nichts bekommt einen Mount"),
                 T("Text and hex editor with find and replace, a Markdown viewer; links never followed", "Text- und Hex-Editor mit Suchen und Ersetzen, eine Markdown-Ansicht; Links werden nie verfolgt"),
             ],
             "captions": [
                 T("Files shows a sandbox's folders beside your own: copy, move or drag files across, and edit them in the app's text or hex editor.",
                   "Dateien zeigt die Ordner einer Sandbox neben Ihren eigenen: Dateien kopieren, verschieben oder hinüberziehen und im Text- oder Hex-Editor der App bearbeiten."),
             ]},
            {"kind": "app", "page": "kits", "heading": T("Kits & plugins and the marketplaces", "Kits & Plugins und die Marktplätze"),
             "bullets": [
                 T("Every kit with what it brings per agent; new kits, skills, plugins and hooks",
                   "Jedes Kit mit dem, was es je Agent mitbringt; neue Kits, Skills, Plugins und Hooks"),
                 T("Marketplaces: sandbox-ai's, the kits', Claude's and Codex's, each plugin with its state",
                   "Marktplätze: die von sandbox-ai, der Kits, von Claude und von Codex, jedes Plugin mit seinem Zustand"),
             ],
             "captions": [
                 T("Kits and plugins shows every kit and, on its Marketplaces tab, every marketplace this machine knows; each change runs sandbox-ai kits, asked first.",
                   "Kits & Plugins zeigt jedes Kit und auf dem Reiter Marktplätze jeden Marktplatz, den dieser Rechner kennt; jede Änderung führt sandbox-ai kits aus, nach Rückfrage."),
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
        "topics": [T("One sandbox per agent", "Eine Sandbox je Agent"), T("The task board", "Das Aufgaben-Board"),
                   T("Limits of teams", "Grenzen von Teams")],
        "scenes": [
            {"kind": "diagram", "diagram": "team", "heading": T("A team works through a task board", "Ein Team arbeitet über ein Aufgaben-Board"),
             "captions": [
                 T("sandbox-ai team starts one sandbox per agent on a shared workspace. The first agent leads and splits the work.",
                   "sandbox-ai team startet eine Sandbox je Agent auf einem gemeinsamen Workspace. Der erste Agent leitet und teilt die Arbeit auf."),
                 T("The task board is an MCP server in a sandbox of its own, without network; every member reaches it through its relay, so the MCP rules hold.",
                   "Das Aufgaben-Board ist ein MCP-Server in einer eigenen Sandbox, ohne Netz; jedes Mitglied erreicht es über sein Relay, die MCP-Regeln gelten also."),
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
                   "Sie können selbst Aufgaben einstellen; team show zeigt die Mitglieder und das Board."),
             ]},
            {"kind": "bullets", "heading": T("Good to know", "Gut zu wissen"),
             "bullets": [
                 T("Members share the workspace: nothing stops two from editing one file",
                   "Mitglieder teilen den Workspace: nichts hindert zwei daran, eine Datei zu ändern"),
                 T("The board does not know who calls it: a member names itself", "Das Board weiß nicht, wer es aufruft: ein Mitglied nennt sich selbst"),
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

# --- the series: one short video per page of the desktop app ----------------------------------------------------
#
# SERIES["pages"]["videos"] holds one video per key of APP_PAGES, in the app's order. A video has the "page" it
# is about, "topics" for its title card, "intro" (the narration over the title card) and "scenes" (the kinds
# above; an "app" scene may name a "shot": a state captured by capture_app.py, such as "files-editor", else the
# page itself; "focus" [x0, y0, x1, y1] (fractions of the picture) frames a part of it, "zoom" shows only that
# part, larger; a scene without bullets shows the picture over the whole width). The narration of the German
# videos is spoken (narrate.py, with the English IT terms of pronunciation.py), so it holds no paths, options or
# symbols: what is typed goes into the bullets.
SERIES = {
    "pages": {
        "title": T("The desktop app, page by page", "Die Desktop-App, Seite für Seite"),
        "part": T("Part", "Teil"),
        "next": T("Next", "Als Nächstes"),
        "last": T("That was the last page", "Das war die letzte Seite"),
        "outro_bullets": [
            T("Every part of a page has a (?) with a short explanation", "Jeder Teil einer Seite hat ein (?) mit einer kurzen Erklärung"),
            T("Help (F1): every page in detail, with tours", "Hilfe (F1): jede Seite im Detail, mit Touren"),
            T("The app runs the sandbox-ai commands: the same works in a terminal", "Die App führt die sandbox-ai-Befehle aus: dasselbe geht im Terminal"),
        ],
        "outro_captions": [
            T("Every part of the page has a small question mark that explains it, and Help has every page in detail.",
              "Jeder Teil der Seite hat ein kleines Fragezeichen, das ihn erklärt, und die Hilfe beschreibt jede Seite im Detail."),
        ],
        "videos": [
            {
                "id": "launch", "page": "launch",
                "topics": [T("Host, agent, model and preset", "Host, Agent, Modell und Preset"),
                           T("Every option of the launcher", "Jede Option des Starters"),
                           T("Dry run, start, session", "Probelauf, Start, Session")],
                "intro": [T("The Start page starts one agent in one sandbox: you choose what it may reach, the app runs sandbox-ai with exactly these options.",
                            "Die Seite Start startet einen Agenten in einer Sandbox: Sie wählen, was er erreichen darf, und die App führt sandbox-ai mit genau diesen Optionen aus.")],
                "scenes": [
                    {"kind": "app", "page": "launch", "heading": T("Who starts where", "Wer startet wo"),
                     "focus": [0.16, 0.15, 0.92, 0.31],
                     "bullets": [
                         T("Host: this machine or one of `sandbox-ai hosts`", "Host: dieser Rechner oder einer aus `sandbox-ai hosts`"),
                         T("Agent: Claude Code, Codex or OpenCode", "Agent: Claude Code, Codex oder OpenCode"),
                         T("Model: the provider's, or your own model server", "Modell: das des Anbieters oder Ihr eigener Modellserver"),
                         T("Preset with Gallery…; profiles keep a whole form", "Preset mit Galerie…; Profile merken ein ganzes Formular"),
                     ],
                     "captions": [
                         T("The row at the top chooses the host, the agent and its model: the provider's, or a model server of your own from the page LLM servers.",
                           "Die Zeile oben wählt den Host, den Agenten und sein Modell: das des Anbieters oder einen eigenen Modellserver von der Seite LLM-Server."),
                         T("A preset fills the form with a tested setup; the gallery shows each one as a picture of its board. A profile keeps a whole form under a name.",
                           "Ein Preset füllt das Formular mit einem erprobten Setup; die Galerie zeigt jedes als Bild seines Boards. Ein Profil merkt sich ein ganzes Formular unter einem Namen."),
                     ]},
                    {"kind": "app", "page": "launch", "shot": "launch-network", "heading": T("Every option, in groups", "Jede Option, in Gruppen"),
                     "zoom": [0.16, 0.3, 0.99, 0.76],
                     "bullets": [
                         T("Built from the launcher's own option parser", "Gebaut aus dem Optionsparser des Starters"),
                         T("Workspace, network, proxy, Git, login, display, hardware, limits, engine", "Arbeitsordner, Netzwerk, Proxy, Git, Anmeldung, Anzeige, Hardware, Grenzen, Engine"),
                         T("Forbidden by the administrator's policy: greyed out", "Von der Richtlinie des Administrators verboten: ausgegraut"),
                         T("New: services beside the agent (`--service`), a model's parameters (`--llm-param`)", "Neu: Services neben dem Agenten (`--service`), Parameter eines Modells (`--llm-param`)"),
                     ],
                     "captions": [
                         T("The tabs below hold every option of the launcher, built from its own option parser: a new option of sandbox-ai appears here without a new app.",
                           "Die Reiter darunter enthalten jede Option des Starters, gebaut aus seinem eigenen Optionsparser: Eine neue Option von sandbox-ai erscheint hier ohne neue App."),
                         T("On the Network tab you open exactly the destinations the task needs, such as GitHub or the Python package index. Everything else stays closed.",
                           "Auf dem Reiter Netzwerk öffnen Sie genau die Ziele, die die Aufgabe braucht, etwa GitHub oder den Python-Paketindex. Alles andere bleibt zu."),
                         T("A service, a database for example, runs beside the agent in the same pod and behind the same firewall; a local model gets its parameters per start.",
                           "Ein Service, etwa eine Datenbank, läuft neben dem Agenten im selben Pod und hinter derselben Firewall; ein lokales Modell bekommt seine Parameter je Start."),
                     ]},
                    {"kind": "app", "page": "launch", "heading": T("Check, then start", "Erst prüfen, dann starten"),
                     "focus": [0.16, 0.79, 0.99, 0.92],
                     "bullets": [
                         T("The exact command line, to copy into a terminal", "Die genaue Kommandozeile, zum Kopieren ins Terminal"),
                         T("Dry run: the launcher checks everything, starts nothing", "Probelauf: Der Starter prüft alles und startet nichts"),
                         T("Start in the app or a terminal window; as a session it keeps running", "Start in der App oder im Terminalfenster; als Session läuft er weiter"),
                         T("Save as project configuration… writes `.sandbox-ai.json`, asked first", "Als Projektkonfiguration speichern… schreibt `.sandbox-ai.json`, nach Rückfrage"),
                     ],
                     "captions": [
                         T("Below the form is the exact command the app will run. Dry run lets the launcher check everything and show its plan, without starting anything.",
                           "Unter dem Formular steht der genaue Befehl, den die App ausführt. Der Probelauf lässt den Starter alles prüfen und seinen Plan zeigen, ohne etwas zu starten."),
                         T("Start runs it in a tab of the app or in your terminal. As a session, named by a codename, it keeps running when the window closes.",
                           "Start führt ihn in einem Tab der App oder in Ihrem Terminal aus. Als Session, benannt mit einem Codenamen, läuft er weiter, wenn das Fenster schließt."),
                         T("Save as project configuration writes the project file and trusts it, but only after showing it: every later start in the project applies it.",
                           "Als Projektkonfiguration speichern schreibt die Projektdatei und vertraut ihr, aber erst, nachdem es sie gezeigt hat: Jeder spätere Start im Projekt wendet sie an."),
                     ]},
                    {"kind": "terminal", "heading": T("A start says what it does", "Ein Start sagt, was er tut"),
                     "steps": [
                         {"comment": T("in the start's terminal tab, before the agent appears",
                                       "im Terminal-Tab des Starts, bevor der Agent erscheint"),
                          "cmd": "sandbox-ai claude --mcp fetch", "example": True, "out": [
                              "agent-sandbox: starting the MCP server fetch (its image is pulled once, its sandbox started)…",
                              "agent-sandbox: starting the MCP server fetch (its image is pulled once, its sandbox started): done in 7.8 s",
                              "agent-sandbox: preparing the kits and plugins…",
                              "agent-sandbox: starting the sandbox: its firewall, connection proxy and the agent…",
                              "agent-sandbox: ready in 11 s",
                          ]},
                     ],
                     "captions": [
                         T("A start shows progress lines in its terminal: each step before the agent appears, how long a slow one took, and when it is ready. Tokens and passwords in a line are masked.",
                           "Ein Start zeigt Fortschrittszeilen in seinem Terminal: jeden Schritt, bevor der Agent erscheint, wie lange ein langsamer dauerte, und wann er bereit ist. Tokens und Passwörter in einer Zeile werden maskiert."),
                         T("What takes minutes, an image variant or new MCP servers, is prepared first in a tab of its own, Preparing the start, each step numbered and timed; then the start goes on.",
                           "Was Minuten dauert, eine Image-Variante oder neue MCP-Server, wird zuerst in einem eigenen Tab vorbereitet, Der Start wird vorbereitet, jeder Schritt nummeriert und mit Zeit; dann geht der Start weiter."),
                     ]},
                ],
            },
            {
                "id": "board", "page": "board",
                "topics": [T("A sandbox as parts you connect", "Eine Sandbox als verbundene Teile"),
                           T("Presets and what they need", "Presets und was sie brauchen"),
                           T("Findings, score, Apply", "Befunde, Wert, Übernehmen")],
                "intro": [T("The board shows one sandbox as a graph of parts: the agent, its workspaces, the proxy, the network and everything else it may use.",
                            "Das Board zeigt eine Sandbox als Graph aus Teilen: den Agenten, seine Workspaces, den Proxy, das Netz und alles andere, was er benutzen darf.")],
                "scenes": [
                    {"kind": "app", "page": "board", "heading": T("Parts and connections", "Teile und Verbindungen"),
                     "zoom": [0.14, 0.13, 0.99, 0.95],
                     "bullets": [
                         T("Drag a part from the list onto the board, or double-click it", "Ein Teil aus der Liste aufs Board ziehen oder doppelklicken"),
                         T("Drag from a part's port onto another to connect them", "Vom Anschluss eines Teils auf ein anderes ziehen, um sie zu verbinden"),
                         T("Drop folders from your file manager: workspace or mount", "Ordner aus dem Dateimanager fallen lassen: Workspace oder Mount"),
                         T("Every change is one step of Undo", "Jede Änderung ist ein Schritt von Rückgängig"),
                     ],
                     "captions": [
                         T("On the left is the list of parts. Drag one onto the board and the guide asks the few things it needs, each with what it means.",
                           "Links ist die Liste der Teile. Ziehen Sie eines aufs Board, und der Guide fragt die wenigen Dinge, die es braucht, jeweils mit ihrer Bedeutung."),
                         T("Connect parts by dragging from one port to another. A workspace, a mount or an MCP server counts only while it is connected to the agent.",
                           "Teile verbinden Sie, indem Sie von einem Anschluss zum anderen ziehen. Ein Workspace, ein Mount oder ein MCP-Server zählt nur, solange er mit dem Agenten verbunden ist."),
                     ]},
                    {"kind": "app", "page": "board", "shot": "board-gallery", "heading": T("New from preset…", "Neu aus Vorlage…"),
                     "zoom": [0.06, 0.03, 0.97, 0.99],
                     "captions": [
                         T("New from preset opens the gallery: tested setups in categories, from offline work to Tor, each with its board, its score and what it leaves to fill in.",
                           "Neu aus Vorlage öffnet die Galerie: erprobte Setups in Kategorien, von der Offline-Arbeit bis zu Tor, jedes mit seinem Board, seinem Wert und dem, was noch auszufüllen ist."),
                         T("What a preset needs is prepared before the start: a missing identity, an image variant, a Tor profile, MCP servers from their catalogs.",
                           "Was ein Preset braucht, wird vor dem Start vorbereitet: eine fehlende Identität, eine Image-Variante, ein Tor-Profil, MCP-Server aus ihren Katalogen."),
                     ]},
                    {"kind": "app", "page": "board", "shot": "board-preset", "heading": T("Findings, score and Apply", "Befunde, Wert und Übernehmen"),
                     "focus": [0.27, 0.14, 0.79, 0.24],
                     "bullets": [
                         T("Findings, badges and a security score follow every change", "Befunde, Abzeichen und ein Sicherheitswert folgen jeder Änderung"),
                         T("The launcher's own verdict: a dry run in the background", "Das eigene Urteil des Starters: ein Probelauf im Hintergrund"),
                         T("Apply… shows the file and asks; Cancel is the default", "Übernehmen… zeigt die Datei und fragt; Abbrechen ist vorgewählt"),
                         T("Clone: the setup as a new project or a new session", "Klonen: das Setup als neues Projekt oder neue Session"),
                     ],
                     "captions": [
                         T("Above the board are the command line and the security score; findings, badges and the launcher's own verdict follow every change.",
                           "Über dem Board stehen die Kommandozeile und der Sicherheitswert; Befunde, Abzeichen und das Urteil des Starters folgen jeder Änderung."),
                         T("Apply shows the project file and what changes, and asks first; then it writes the file and trusts it. Dry run and Start are beside it.",
                           "Übernehmen zeigt die Projektdatei und was sich ändert, und fragt zuerst; dann schreibt es die Datei und vertraut ihr. Probelauf und Start stehen daneben."),
                         T("Clone takes the same setup to another folder as a new project, or starts it as a new session.",
                           "Klonen nimmt dasselbe Setup als neues Projekt in einen anderen Ordner mit oder startet es als neue Session."),
                     ]},
                    {"kind": "bullets", "heading": T("Services, MCP servers and models", "Services, MCP-Server und Modelle"),
                     "bullets": [
                         T("Service (container): one of your images beside the agent, marked UNCHECKED and scored", "Service (Container): eines Ihrer Images neben dem Agenten, als UNGEPRÜFT markiert und bewertet"),
                         T("MCP part: memory, CPUs, processes, /tmp; the server's own as default; Defaults for every start…", "MCP-Teil: Speicher, CPUs, Prozesse, /tmp; die des Servers als Vorgabe; Standard für jeden Start…"),
                         T("LLM part: the model's sampling parameters and the loop guard", "LLM-Teil: die Sampling-Parameter des Modells und der Loop Guard"),
                     ],
                     "captions": [
                         T("A service part runs one of your own images beside the agent, a database for example. Nobody has reviewed such an image: it is marked unchecked and lowers the score.",
                           "Ein Service-Teil lässt eines Ihrer eigenen Images neben dem Agenten laufen, etwa eine Datenbank. Niemand hat so ein Image geprüft: Es ist als ungeprüft markiert und senkt den Wert."),
                         T("An MCP part has limits of its own for memory, CPUs and processes, and an LLM part the model's parameters and the loop guard.",
                           "Ein MCP-Teil hat eigene Grenzen für Speicher, CPUs und Prozesse, und ein LLM-Teil die Parameter des Modells und den Loop Guard."),
                     ]},
                ],
            },
            {
                "id": "sandboxes", "page": "sandboxes",
                "topics": [T("Every start, grouped with its parts", "Jeder Start, mit seinen Teilen gruppiert"),
                           T("Change a running sandbox", "Eine laufende Sandbox ändern"),
                           T("Kill switch and emergency stop", "Kill-Switch und Notaus")],
                "intro": [T("Sandboxes and sessions lists every sandbox of your account, here and on your hosts, with a button for each thing you can do with it.",
                            "Sandboxen & Sitzungen listet jede Sandbox Ihres Kontos, hier und auf Ihren Hosts, mit einem Knopf für alles, was Sie damit tun können.")],
                "scenes": [
                    {"kind": "app", "page": "sandboxes", "heading": T("One tree of everything", "Ein Baum für alles"),
                     "focus": [0.16, 0.11, 0.99, 0.62],
                     "bullets": [
                         T("Hosts, under them their starts: runs, sessions, MCP servers", "Hosts, darunter ihre Starts: Läufe, Sessions, MCP-Server"),
                         T("A start with its MCP servers, helpers and team as one group", "Ein Start mit MCP-Servern, Helfern und Team als eine Gruppe"),
                         T("State, proxy, memory; asking when a question waits", "Zustand, Proxy, Speicher; fragt, wenn eine Frage wartet"),
                     ],
                     "captions": [
                         T("Every host is a branch, and under it its starts: one-off runs, sessions, MCP servers. A start stands together with everything that belongs to it.",
                           "Jeder Host ist ein Zweig, darunter seine Starts: einzelne Läufe, Sessions, MCP-Server. Ein Start steht zusammen mit allem, was zu ihm gehört."),
                         T("The columns show the agent, the backend, when it started, its state and memory. A start whose question waits is marked as asking.",
                           "Die Spalten zeigen Agent, Backend, Startzeit, Zustand und Speicher. Ein Start, dessen Frage wartet, ist als fragend markiert."),
                     ]},
                    {"kind": "app", "page": "sandboxes", "heading": T("Sessions and running sandboxes", "Sessions und laufende Sandboxen"),
                     "focus": [0.16, 0.64, 0.99, 0.92],
                     "bullets": [
                         T("Attach, in a window or in the app; pause, resume, stop", "Verbinden, im Fenster oder in der App; pausieren, fortsetzen, stoppen"),
                         T("While it runs: allow, revoke, devices, limits", "Im Lauf: erlauben, zurücknehmen, Geräte, Grenzen"),
                         T("Add a workspace or an MCP server without a restart", "Einen Workspace oder MCP-Server ohne Neustart hinzufügen"),
                         T("Copy or move a session to another host, checked there first", "Eine Session auf einen anderen Host kopieren oder verschieben, dort zuerst geprüft"),
                     ],
                     "captions": [
                         T("A session can be attached in a window or in the app, paused and resumed. Its HOME is kept, so the agent continues where it stopped.",
                           "Eine Session lässt sich im Fenster oder in der App verbinden, pausieren und fortsetzen. Ihr HOME bleibt erhalten, der Agent macht also dort weiter, wo er aufgehört hat."),
                         T("While a sandbox runs, you change what it may reach, give it a device, more memory, another workspace or an MCP server, without a restart.",
                           "Während eine Sandbox läuft, ändern Sie, was sie erreichen darf, geben ihr ein Gerät, mehr Speicher, einen weiteren Workspace oder einen MCP-Server, ohne Neustart."),
                         T("Move to host checks what the session will meet on the other host, starts it there and only then stops it here. Report writes a report of the start.",
                           "Auf Host verschieben prüft, was die Session auf dem anderen Host vorfindet, startet sie dort und stoppt sie erst dann hier. Bericht schreibt einen Bericht über den Start."),
                     ]},
                    {"kind": "app", "page": "sandboxes", "shot": "sandboxes-cut", "heading": T("Kill switch and emergency stop", "Kill-Switch und Notaus"),
                     "zoom": [0.45, 0.52, 1.0, 0.97],
                     "bullets": [
                         T("Cut the internet, the local network, all but the model API, or everything", "Internet, lokales Netz, alles außer der Modell-API oder alles kappen"),
                         T("With the start's MCP servers, helpers and team", "Mit den MCP-Servern, Helfern und dem Team des Starts"),
                         T("Lift the cut gives back exactly what it had", "Kappung aufheben gibt genau das zurück, was er hatte"),
                         T("Emergency stop, Ctrl+Shift+X: every sandbox, every host", "Notaus, Strg+Umschalt+X: jede Sandbox, jeder Host"),
                     ],
                     "captions": [
                         T("The kill switch cuts the network of the selected sandboxes at once, without a question: the internet, the local network, everything but the model API, or everything.",
                           "Der Kill-Switch kappt das Netz der gewählten Sandboxen sofort, ohne Rückfrage: das Internet, das lokale Netz, alles außer der Modell-API, oder alles."),
                         T("The firewall, the proxy and the supervisor hold the cut, out of the agent's reach, and open connections end. Lift the cut gives back exactly what it had.",
                           "Firewall, Proxy und Supervisor halten die Kappung, außerhalb der Reichweite des Agenten, und offene Verbindungen enden. Kappung aufheben gibt genau das zurück, was er hatte."),
                         T("The emergency stop, the red button at the top or Ctrl Shift X, cuts every sandbox here and on all your hosts.",
                           "Der Notaus, der rote Knopf oben oder Strg Umschalt X, kappt jede Sandbox hier und auf allen Ihren Hosts."),
                     ]},
                ],
            },
            {
                "id": "approvals", "page": "approvals",
                "topics": [T("Questions, and why they are asked", "Fragen, und warum sie gestellt werden"),
                           T("Blocked: every refusal of a run", "Blockiert: jede Ablehnung eines Laufs"),
                           T("Allowed now, remembered, history", "Jetzt erlaubt, gemerkt, Verlauf")],
                "intro": [T("When an agent wants something its options do not allow, its sandbox asks you instead of failing. The Approvals page is where you answer.",
                            "Wenn ein Agent etwas will, das seine Optionen nicht erlauben, fragt seine Sandbox Sie, statt zu scheitern. Auf der Seite Freigaben antworten Sie.")],
                "scenes": [
                    {"kind": "app", "page": "approvals", "heading": T("Open questions", "Offene Fragen"),
                     "zoom": [0.14, 0.07, 1.0, 0.66],
                     "bullets": [
                         T("A connection, a lookup, a push, a request a rule asks about, a page to open", "Eine Verbindung, ein Lookup, ein Push, eine Anfrage, nach der eine Regel fragt, eine Seite zum Öffnen"),
                         T("The selected question says why it is asked", "Die gewählte Frage sagt, warum gefragt wird"),
                         T("Allow once, for this run or always; deny; block always", "Einmal, für diesen Lauf oder immer erlauben; ablehnen; immer sperren"),
                     ],
                     "captions": [
                         T("The questions of every sandbox, here and on your hosts, wait at the top: a connection, a name lookup, a push with privacy warnings, a page to open.",
                           "Oben warten die Fragen jeder Sandbox, hier und auf Ihren Hosts: eine Verbindung, ein Lookup, ein Push mit Datenschutz-Warnungen, eine Seite zum Öffnen."),
                         T("The selected question says why it is asked. You allow it once, for this run or always for the project, and the app confirms what you allow first.",
                           "Die gewählte Frage sagt, warum sie gestellt wird. Sie erlauben sie einmal, für diesen Lauf oder immer für das Projekt, und die App bestätigt vorher, was Sie erlauben."),
                         T("Without an answer within a minute the request is refused, refused at once for two minutes, then asked again: no answer is no decision.",
                           "Ohne Antwort innerhalb einer Minute wird die Anfrage abgelehnt, zwei Minuten lang sofort abgelehnt und dann wieder gefragt: Keine Antwort ist keine Entscheidung."),
                     ]},
                    {"kind": "app", "page": "approvals", "shot": "approvals-blocked", "heading": T("Blocked", "Blockiert"),
                     "zoom": [0.14, 0.3, 1.0, 0.94],
                     "bullets": [
                         T("Every refusal of the run, with its reason and the process", "Jede Ablehnung des Laufs, mit Grund und Prozess"),
                         T("Allow for this run, Allow always…, Ask again, Deny for this run", "Für diesen Lauf erlauben, Immer erlauben…, Wieder fragen, Für diesen Lauf ablehnen"),
                         T("The security mode, the policy and the kill switch still hold", "Sicherheitsmodus, Richtlinie und Kill-Switch gelten weiter"),
                     ],
                     "captions": [
                         T("Below the questions the page is about one sandbox. Blocked lists everything it was refused, with the reason and the process that tried.",
                           "Unter den Fragen geht es um eine Sandbox. Blockiert listet alles, was ihr verweigert wurde, mit dem Grund und dem Prozess, der es versucht hat."),
                         T("From there you allow it for this run, allow it always as an exception rule, shown before it is written, or let the next try ask again.",
                           "Von dort erlauben Sie es für diesen Lauf, immer als Ausnahmeregel, die vor dem Schreiben gezeigt wird, oder lassen den nächsten Versuch wieder fragen."),
                         T("The network settings open destinations, but they do not change what a ruleset decides: a request a rule asks about is asked even when its host is open.",
                           "Die Netzeinstellungen öffnen Ziele, ändern aber nicht, was ein Regelwerk entscheidet: Eine Anfrage, nach der eine Regel fragt, wird gefragt, auch wenn ihr Host offen ist."),
                     ]},
                    {"kind": "bullets", "heading": T("Allowed now, remembered, history", "Jetzt erlaubt, gemerkt, Verlauf"),
                     "bullets": [
                         T("Allowed now: what the run may reach beyond its options; Revoke… takes it back", "Jetzt erlaubt: was der Lauf über seine Optionen hinaus erreicht; Zurücknehmen… nimmt es zurück"),
                         T("Remembered: answers that hold for later starts; Forget… asks again", "Gemerkt: Antworten für spätere Starts; Vergessen… fragt wieder"),
                         T("History: every question with its answer", "Verlauf: jede Frage mit ihrer Antwort"),
                         T("Notifications only when turned on in Settings; one answer settles a question everywhere", "Benachrichtigungen nur, wenn in den Einstellungen eingeschaltet; eine Antwort erledigt eine Frage überall"),
                     ],
                     "captions": [
                         T("Allowed now shows what the run may reach beyond its start options, and takes it back. Remembered keeps your answers for later starts.",
                           "Jetzt erlaubt zeigt, was der Lauf über seine Startoptionen hinaus erreichen darf, und nimmt es zurück. Gemerkt hält Ihre Antworten für spätere Starts."),
                         T("The page keeps reading the questions while the window is closed to the tray; pop-up notifications are off unless you turn them on. When you are unsure, allow once.",
                           "Die Seite liest die Fragen weiter, auch wenn das Fenster im Infobereich ist; Benachrichtigungen sind aus, außer Sie schalten sie ein. Wenn Sie unsicher sind, erlauben Sie einmal."),
                     ]},
                ],
            },
            {
                "id": "terminals", "page": "terminals",
                "topics": [T("Terminal tabs in the app", "Terminal-Tabs in der App"),
                           T("Sessions in tmux", "Sessions in tmux"), T("xterm.js, pinned and checked", "xterm.js, festgelegt und geprüft")],
                "intro": [T("Terminals shows the agents started in the app and the sessions you attach to, each in a tab of its own.",
                            "Terminals zeigt die in der App gestarteten Agenten und die Sessions, mit denen Sie sich verbinden, jede in einem eigenen Tab.")],
                "scenes": [
                    {"kind": "app", "page": "terminals", "heading": T("A tab for each start", "Ein Tab für jeden Start"),
                     "bullets": [
                         T("Agents started in the app, sessions attached in the app", "In der App gestartete Agenten, in der App verbundene Sessions"),
                         T("One tab per start; its MCP servers and helpers open none", "Ein Tab je Start; seine MCP-Server und Helfer öffnen keinen"),
                         T("Sessions, below the tabs: every start, here and on your hosts", "Sitzungen, unter den Tabs: jeder Start, hier und auf Ihren Hosts"),
                     ],
                     "captions": [
                         T("A start opens one tab, for its agent. Its MCP servers, helpers and the other members of a team open none, unless you ask for one.",
                           "Ein Start öffnet einen Tab, für seinen Agenten. Seine MCP-Server, Helfer und die übrigen Mitglieder eines Teams öffnen keinen, außer Sie wollen es."),
                         T("Below the tabs, Sessions lists every start that runs here or on a host, however it was started; Open terminal attaches to it.",
                           "Unter den Tabs listet Sitzungen jeden Start, der hier oder auf einem Host läuft, egal wie er gestartet wurde; Terminal öffnen verbindet sich damit."),
                     ]},
                    {"kind": "bullets", "heading": T("Tabs you arrange, terminals that come back", "Tabs zum Ordnen, Terminals, die wiederkommen"),
                     "bullets": [
                         T("Drag a tab to move it; Alt+1 … Alt+0 show the first ten", "Einen Tab ziehen, um ihn zu verschieben; Alt+1 … Alt+0 zeigen die ersten zehn"),
                         T("At the app's start: the terminals of running sessions reopened, in their order", "Beim Start der App: die Terminals laufender Sessions wieder geöffnet, in ihrer Reihenfolge"),
                         T("Only attached, never started again; one that ended meanwhile is named", "Nur verbunden, nie neu gestartet; eine inzwischen beendete wird genannt"),
                         T("Settings: Reopen the terminals of running sessions at start (on)", "Einstellungen: Terminals laufender Sitzungen beim Start wieder öffnen (an)"),
                     ],
                     "captions": [
                         T("Tabs move by dragging them, and Alt with a digit from one to zero shows one of the first ten.",
                           "Tabs verschieben Sie, indem Sie sie ziehen, und Alt mit einer Ziffer von eins bis null zeigt einen der ersten zehn."),
                         T("When the app starts again, it reopens the terminals of the sessions that still run, in their order. They are only attached, never started again; a session that ended meanwhile is named.",
                           "Startet die App neu, öffnet sie die Terminals der Sessions, die noch laufen, wieder, in ihrer Reihenfolge. Sie werden nur verbunden, nie neu gestartet; eine inzwischen beendete Session wird genannt."),
                     ]},
                    {"kind": "bullets", "heading": T("Sessions and tmux", "Sessions und tmux"),
                     "bullets": [
                         T("The agent runs in the tmux window `agent`, a shell in `shell`", "Der Agent läuft im tmux-Fenster `agent`, eine Shell in `shell`"),
                         T("Ctrl-b d detaches: the session keeps running", "Strg-b d trennt: Die Session läuft weiter"),
                         T("Ctrl+W and Ctrl+Tab go to the terminal on this page", "Strg+W und Strg+Tab gehen auf dieser Seite an das Terminal"),
                     ],
                     "captions": [
                         T("In a session the agent runs in tmux: one window for the agent, one for a shell. Control b, then d, detaches; the session keeps running.",
                           "In einer Session läuft der Agent in tmux: ein Fenster für den Agenten, eines für eine Shell. Strg b, dann d, trennt; die Session läuft weiter."),
                         T("Attach on the page Sandboxes and sessions brings it back, in the app or in a terminal window.",
                           "Verbinden auf der Seite Sandboxen & Sitzungen holt sie zurück, in der App oder in einem Terminalfenster."),
                     ]},
                    {"kind": "bullets", "heading": T("Copy, paste and logins", "Kopieren, Einfügen und Anmelden"),
                     "bullets": [
                         T("Ctrl+Shift+C and Ctrl+Shift+V (Cmd on macOS), and a right-click menu", "Strg+Umschalt+C und Strg+Umschalt+V (Cmd auf macOS) und ein Rechtsklick-Menü"),
                         T("Links broken over rows are joined; Ctrl+click opens one", "Über Zeilen umbrochene Links werden zusammengefügt; Strg+Klick öffnet einen"),
                         T("A paste guard: control characters, several lines, more than 1 MiB are asked first", "Ein Schutz beim Einfügen: Steuerzeichen, mehrere Zeilen, mehr als 1 MiB werden erst erfragt"),
                         T("Logins in the app's login browser: provider pages only, the code goes back into the tab", "Anmeldungen im Login-Browser der App: nur Seiten des Anbieters, der Code geht zurück in den Tab"),
                     ],
                     "captions": [
                         T("Control Shift C copies and Control Shift V pastes, and a right click opens a menu. Pasting control characters, several lines or a lot of text is asked first.",
                           "Strg Umschalt C kopiert und Strg Umschalt V fügt ein, und ein Rechtsklick öffnet ein Menü. Steuerzeichen, mehrere Zeilen oder viel Text einzufügen wird zuerst erfragt."),
                         T("A login opens in the app's own login browser. It opens only the provider's pages, never this machine or your network, and hands the code back to the terminal.",
                           "Eine Anmeldung öffnet sich im eigenen Login-Browser der App. Er öffnet nur die Seiten des Anbieters, nie diesen Rechner oder Ihr Netz, und gibt den Code an das Terminal zurück."),
                     ]},
                    {"kind": "bullets", "heading": T("xterm.js, pinned and checked", "xterm.js, festgelegt und geprüft"),
                     "bullets": [
                         T("The tabs use xterm.js in QtWebEngine", "Die Tabs nutzen xterm.js in QtWebEngine"),
                         T("Downloaded once, checked against its pinned digest", "Einmal geladen, gegen seinen festgelegten Digest geprüft"),
                         T("The page loads only local files", "Die Seite lädt nur lokale Dateien"),
                         T("Without it: a terminal window instead (AppImage, macOS app)", "Ohne: stattdessen ein Terminalfenster (AppImage, macOS-App)"),
                     ],
                     "captions": [
                         T("The tabs use xterm.js. It is not part of the app: it is downloaded once and checked against its pinned digest, and the page reaches nothing remote.",
                           "Die Tabs nutzen xterm.js. Es gehört nicht zur App: Es wird einmal geladen und gegen seinen festgelegten Digest geprüft, und die Seite erreicht nichts im Netz."),
                         T("Without it, as in the AppImage and the macOS app, every command opens in a terminal window instead.",
                           "Ohne es, wie im AppImage und in der macOS-App, öffnet sich jeder Befehl stattdessen in einem Terminalfenster."),
                     ]},
                ],
            },
            {
                "id": "connections", "page": "connections",
                "topics": [T("The proxy's log", "Das Protokoll des Proxys"), T("Filters and refusals", "Filter und Ablehnungen"),
                           T("The option that allows it", "Die Option, die es erlaubt")],
                "intro": [T("Every connection of a sandbox passes its proxy, which logs each request. Connections shows that log.",
                            "Jede Verbindung einer Sandbox passiert ihren Proxy, der jede Anfrage protokolliert. Verbindungen zeigt dieses Protokoll.")],
                "scenes": [
                    {"kind": "app", "page": "connections", "heading": T("Every request, push and refusal", "Jede Anfrage, jeder Push, jede Ablehnung"),
                     "zoom": [0.14, 0.1, 1.0, 0.8],
                     "bullets": [
                         T("The log of a project and run, following while it runs", "Das Protokoll eines Projekts und Laufs, mitlaufend"),
                         T("Filter by host, path or method", "Filter nach Host, Pfad oder Methode"),
                         T("Only refused and errors: what was not let through, in red", "Nur Abgelehntes und Fehler: was nicht durchgelassen wurde, in Rot"),
                     ],
                     "captions": [
                         T("Pick the project and the run, and the log follows while the sandbox runs: the model calls, downloads, Git, and what was refused, in red.",
                           "Wählen Sie Projekt und Lauf, und das Protokoll läuft mit, während die Sandbox läuft: die Modellaufrufe, Downloads, Git, und was abgelehnt wurde, in Rot."),
                         T("Here a connection over SSH was refused, a ruleset denied a gist, and the privacy check stopped a push that carried an access key.",
                           "Hier wurde eine Verbindung über SSH abgelehnt, ein Regelwerk hat einen Gist verweigert, und die Datenschutzprüfung hat einen Push mit einem Zugangsschlüssel gestoppt."),
                     ]},
                    {"kind": "app", "page": "connections", "shot": "connections-record", "heading": T("The full record", "Der vollständige Eintrag"),
                     "zoom": [0.14, 0.28, 1.0, 0.92],
                     "bullets": [
                         T("Method, URL, headers and bodies, credentials redacted", "Methode, URL, Header und Bodies, Zugangsdaten geschwärzt"),
                         T("The rule that decided, and the answer", "Die Regel, die entschied, und die Antwort"),
                         T("A refusal comes with the option that would allow it", "Eine Ablehnung kommt mit der Option, die sie erlauben würde"),
                     ],
                     "captions": [
                         T("Select a line to see its full record: method, URL, headers and bodies with credentials redacted, the rule that decided and the answer.",
                           "Wählen Sie eine Zeile, um den ganzen Eintrag zu sehen: Methode, URL, Header und Bodies mit geschwärzten Zugangsdaten, die Regel, die entschied, und die Antwort."),
                         T("After a failed download, look here first, then open only what the task needs: each suggested option opens the sandbox a little further.",
                           "Nach einem gescheiterten Download schauen Sie zuerst hier und öffnen dann nur, was die Aufgabe braucht: Jede vorgeschlagene Option öffnet die Sandbox ein Stück weiter."),
                         T("Texts that come from the sandbox, such as hosts and URLs, are shown escaped. In Activity, Show in Connections opens a network event here, filtered to its host.",
                           "Texte aus der Sandbox, etwa Hosts und URLs, werden maskiert gezeigt. In Aktivität öffnet In Verbindungen zeigen ein Netzereignis hier, gefiltert auf seinen Host."),
                     ]},
                ],
            },
            {
                "id": "activity", "page": "activity",
                "topics": [T("What the agents did, in order", "Was die Agenten taten, der Reihe nach"),
                           T("Canaries", "Canaries"), T("Safeguards and the fix", "Schutzmaßnahmen und die Abhilfe")],
                "intro": [T("Activity shows what the agents did in a run, a session or every run of a project, in order and live.",
                            "Aktivität zeigt, was die Agenten in einem Lauf, einer Session oder allen Läufen eines Projekts getan haben, der Reihe nach und live.")],
                "scenes": [
                    {"kind": "app", "page": "activity", "heading": T("The timeline", "Die Zeitleiste"),
                     "zoom": [0.14, 0.08, 1.0, 0.72],
                     "bullets": [
                         T("Prompts, replies, tool calls, commands with exit code", "Prompts, Antworten, Tool-Aufrufe, Befehle mit Exit-Code"),
                         T("File changes, network, approvals, MCP calls, tokens", "Dateiänderungen, Netz, Freigaben, MCP-Aufrufe, Tokens"),
                         T("Filter by kind and text; Export… writes what is shown", "Filter nach Art und Text; Exportieren… schreibt, was gezeigt wird"),
                     ],
                     "captions": [
                         T("The timeline holds the prompts, the replies, the tool calls, shell commands with exit code and duration, file changes, network events and the tokens of each turn.",
                           "Die Zeitleiste enthält die Prompts, die Antworten, die Tool-Aufrufe, Befehle mit Exit-Code und Dauer, Dateiänderungen, Netzereignisse und die Tokens jeder Runde."),
                         T("Filter it by kind and text. A network event opens in Connections, a file event in Files. Refusals and errors are red.",
                           "Filtern Sie nach Art und Text. Ein Netzereignis öffnet sich in Verbindungen, ein Dateiereignis in Dateien. Ablehnungen und Fehler sind rot."),
                         T("Everything here may quote the agent, so it is shown as plain text with control characters escaped. Export writes what the filters show as JSON lines or text.",
                           "Alles hier kann den Agenten zitieren, deshalb erscheint es als reiner Text mit maskierten Steuerzeichen. Exportieren schreibt, was die Filter zeigen, als JSON-Zeilen oder Text."),
                     ]},
                    {"kind": "app", "page": "activity", "shot": "activity-safeguards", "heading": T("Canaries and safeguards", "Canaries und Schutzmaßnahmen"),
                     "zoom": [0.14, 0.08, 1.0, 0.5],
                     "bullets": [
                         T("Canaries: every decoy file opened, every canary token that left", "Canaries: jede geöffnete Köderdatei, jeder Canary-Token, der hinausging"),
                         T("Safeguards: everything a safeguard refused or ended, with why", "Schutzmaßnahmen: alles, was eine Schutzmaßnahme ablehnte oder beendete, mit Grund"),
                         T("Copy the fix: the command that would allow it", "Abhilfe kopieren: der Befehl, der es erlauben würde"),
                     ],
                     "captions": [
                         T("The Canaries tab lists every decoy file that was opened and every canary token that left, with the process and the agent's turn at that moment.",
                           "Der Reiter Canaries listet jede geöffnete Köderdatei und jeden Canary-Token, der hinausging, mit dem Prozess und der Runde des Agenten in diesem Moment."),
                         T("Safeguards lists everything a safeguard refused or ended, each with the reason and the command that would allow it. New ones pop up while sandboxes run.",
                           "Schutzmaßnahmen listet alles, was eine Schutzmaßnahme abgelehnt oder beendet hat, mit Grund und dem Befehl, der es erlauben würde. Neue melden sich, während Sandboxen laufen."),
                     ]},
                ],
            },
            {
                "id": "statistics", "page": "statistics",
                "topics": [T("Model calls and tokens", "Modellaufrufe und Tokens"), T("Hosts and credentials", "Hosts und Zugangsdaten"),
                           T("Reports", "Berichte")],
                "intro": [T("Statistics sums up the logs of a project: what the agents called, how much, and where.",
                            "Statistik fasst die Protokolle eines Projekts zusammen: was die Agenten aufgerufen haben, wie viel, und wohin.")],
                "scenes": [
                    {"kind": "app", "page": "statistics", "heading": T("The figures of a project", "Die Zahlen eines Projekts"),
                     "bullets": [
                         T("Model calls and tokens, per run and per agent", "Modellaufrufe und Tokens, je Lauf und je Agent"),
                         T("HTTP requests per host; credential headers sent (names only)", "HTTP-Anfragen je Host; gesendete Zugangsdaten-Header (nur Namen)"),
                         T("Pushes checked and refused, refused connections, programs", "Geprüfte und abgelehnte Pushes, abgelehnte Verbindungen, Programme"),
                     ],
                     "captions": [
                         T("Pick a project's logs and the page counts the model calls and tokens, the requests per host, the pushes, the refusals and the programs started.",
                           "Wählen Sie die Protokolle eines Projekts, und die Seite zählt die Modellaufrufe und Tokens, die Anfragen je Host, die Pushes, die Ablehnungen und die gestarteten Programme."),
                         T("A host you do not expect, or a credential header sent to it, is a reason to look at Connections. Compare two runs before and after a change of options.",
                           "Ein Host, den Sie nicht erwarten, oder ein Zugangsdaten-Header an ihn ist ein Grund, in Verbindungen nachzusehen. Vergleichen Sie zwei Läufe vor und nach einer Änderung der Optionen."),
                     ]},
                    {"kind": "bullets", "heading": T("Report…", "Bericht…"),
                     "bullets": [
                         T("Every run of the log folder, as PDF, HTML or Markdown", "Jeder Lauf des Protokollordners, als PDF, HTML oder Markdown"),
                         T("Every destination, and why it was allowed or refused", "Jedes Ziel, und warum es erlaubt oder abgelehnt wurde"),
                         T("A security report of what was not allowed, the most severe first", "Ein Sicherheitsbericht über das Nicht-Erlaubte, das Schwerste zuerst"),
                         T("No secrets, but hosts, paths and commands: share it with care", "Keine Geheimnisse, aber Hosts, Pfade und Befehle: mit Bedacht teilen"),
                     ],
                     "captions": [
                         T("Report writes one report of the runs: a summary, the statistics, every destination with the reason, and a security report of everything that was not allowed.",
                           "Bericht schreibt einen Bericht über die Läufe: eine Zusammenfassung, die Statistik, jedes Ziel mit dem Grund und einen Sicherheitsbericht über alles, was nicht erlaubt war."),
                         T("It holds no secret, but it names hosts, paths and commands: share it with care.",
                           "Er enthält kein Geheimnis, nennt aber Hosts, Pfade und Befehle: Teilen Sie ihn mit Bedacht."),
                         T("The tokens come from what the model APIs reported in their answers, so the figures match what the provider counts.",
                           "Die Tokens stammen aus dem, was die Modell-APIs in ihren Antworten gemeldet haben, die Zahlen passen also zu dem, was der Anbieter zählt."),
                     ]},
                ],
            },
            {
                "id": "files", "page": "files",
                "topics": [T("Two panes: a sandbox, this machine, a remote system", "Zwei Bereiche: Sandbox, dieser Rechner, ein entferntes System"),
                           T("Copy, move, drag and drop", "Kopieren, verschieben, Drag and Drop"),
                           T("Editor, Markdown, properties", "Editor, Markdown, Eigenschaften")],
                "intro": [T("The file manager shows the workspaces and HOME of every sandbox and session, this machine and remote systems over SSH, without opening a shell in the sandbox.",
                            "Der Dateimanager zeigt die Workspaces und das HOME jeder Sandbox und Session, diesen Rechner und entfernte Systeme über SSH, ohne dass Sie eine Shell in der Sandbox öffnen.")],
                "scenes": [
                    {"kind": "app", "page": "files", "heading": T("Two panes", "Zwei Bereiche"),
                     "bullets": [
                         T("Left: a sandbox's workspaces and HOME; right: this machine", "Links: Workspaces und HOME einer Sandbox; rechts: dieser Rechner"),
                         T("Copy (F5), Move (F6), New folder (F7), Delete (F8), Rename (F2)", "Kopieren (F5), Verschieben (F6), Neuer Ordner (F7), Löschen (F8), Umbenennen (F2)"),
                         T("Drag and drop between the panes and from other programs", "Drag and Drop zwischen den Bereichen und aus anderen Programmen"),
                         T("Take ownership… of what a rootless Podman sandbox wrote", "Besitz übernehmen… von dem, was eine rootless Podman-Sandbox schrieb"),
                         T("Recently visited folders (Alt+Down), the free space below each list", "Zuletzt besuchte Ordner (Alt+Unten), der freie Platz unter jeder Liste"),
                         T("`.` and `..` lead every folder; Home, Up and Refresh as icons; New file (Shift+F4)", "`.` und `..` vor jedem Ordner; Home, Hoch und Neu laden als Symbole; Neue Datei (Umschalt+F4)"),
                     ],
                     "captions": [
                         T("The left pane shows a sandbox's folders, chosen above it; the right pane shows this machine. Copy and move work between them with the function keys.",
                           "Der linke Bereich zeigt die Ordner einer Sandbox, darüber gewählt; der rechte zeigt diesen Rechner. Kopieren und Verschieben gehen zwischen beiden mit den Funktionstasten."),
                         T("Drag and drop works too, between the panes and from other programs. Nothing gets a mount: the files are copied, and nothing is replaced unless you allow it.",
                           "Auch Drag and Drop geht, zwischen den Bereichen und aus anderen Programmen. Nichts bekommt einen Mount: Die Dateien werden kopiert, und nichts wird ersetzt, außer Sie erlauben es."),
                         T("Every folder begins with the dot and the two dots. Each pane remembers its recent folders and shows the space free there.",
                           "Jeder Ordner beginnt mit dem Punkt und den zwei Punkten. Jeder Bereich merkt sich seine letzten Ordner und zeigt den freien Platz dort."),
                     ]},
                    {"kind": "app", "page": "files", "shot": "files-editor", "heading": T("The editor", "Der Editor"),
                     "zoom": [0.15, 0.12, 0.86, 0.89],
                     "captions": [
                         T("A double click opens a file in the app's own editor: text when it is text, else the hex editor. F3 views, F4 edits.",
                           "Ein Doppelklick öffnet eine Datei im eigenen Editor der App: als Text, wenn es Text ist, sonst im Hex-Editor. F3 zeigt an, F4 bearbeitet."),
                         T("Ctrl+F finds, also bytes in the hex view, and Ctrl+H replaces: the match shown, or all of them as one step to undo.",
                           "Strg+F sucht, im Hex-Editor auch Bytes, und Strg+H ersetzt: den gezeigten Treffer oder alle auf einmal, als ein Schritt zum Rückgängigmachen."),
                         T("Save writes back through the same pane, never through a link, and asks first when the agent changed the file meanwhile. Invisible characters are named above the text.",
                           "Speichern schreibt über denselben Bereich zurück, nie über einen Link, und fragt zuerst, wenn der Agent die Datei inzwischen geändert hat. Unsichtbare Zeichen werden über dem Text genannt."),
                     ]},
                    {"kind": "app", "page": "files", "shot": "files-menu", "heading": T("Context menu and properties", "Kontextmenü und Eigenschaften"),
                     "zoom": [0.14, 0.14, 0.6, 0.8],
                     "bullets": [
                         T("Open, view, edit, hex editor; copy, cut, paste", "Öffnen, ansehen, bearbeiten, Hex-Editor; kopieren, ausschneiden, einfügen"),
                         T("Properties (Alt+Enter): owner, group, permissions as checkboxes, a folder's size", "Eigenschaften (Alt+Enter): Besitzer, Gruppe, Rechte als Kästchen, die Größe eines Ordners"),
                         T("Copy path, relative path (Ctrl+Shift+C), path in the sandbox, quoted for the shell", "Pfad, relativen Pfad (Strg+Umschalt+C), Pfad in der Sandbox, für die Shell maskiert kopieren"),
                     ],
                     "captions": [
                         T("The context menu opens, edits, copies and pastes; Copy path in the sandbox gives a path the way the agent sees it.",
                           "Das Kontextmenü öffnet, bearbeitet, kopiert und fügt ein; Pfad in der Sandbox kopieren gibt einen Pfad so, wie der Agent ihn sieht."),
                         T("Properties show owner, group and permissions as checkboxes, and count a folder's size at once.",
                           "Eigenschaften zeigen Besitzer, Gruppe und Rechte als Kästchen und zählen die Größe eines Ordners sofort."),
                     ]},
                    {"kind": "app", "page": "files", "shot": "files-markdown", "heading": T("Markdown, rendered", "Markdown, gerendert"),
                     "zoom": [0.15, 0.1, 0.85, 0.9],
                     "captions": [
                         T("Markdown files open rendered, with tables, task lists, highlighted code and an outline. Nothing in them runs or is loaded from the network, and links are never followed there.",
                           "Markdown-Dateien öffnen sich gerendert, mit Tabellen, Aufgabenlisten, hervorgehobenem Code und einer Gliederung. Nichts darin läuft oder wird aus dem Netz geladen, und Links werden dort nie verfolgt."),
                     ]},
                    # The remote pane: capture_app.py serves the remote HOME with the app's test SFTP server.
                    {"kind": "app", "page": "files", "shot": "files-remote", "heading": T("A remote system over SSH", "Ein entferntes System über SSH"),
                     "zoom": [0.14, 0.13, 1.0, 0.92],
                     "bullets": [
                         T("SSH…: a sandbox-ai host, a Host of `~/.ssh/config`, or user@host", "SSH…: ein Host von sandbox-ai, ein Host aus `~/.ssh/config` oder user@host"),
                         T("SFTP through your OpenSSH: no remote shell, keys and ssh-agent only", "SFTP über Ihr OpenSSH: keine entfernte Shell, nur Schlüssel und ssh-agent"),
                         T("An unknown host's keys shown and asked; a changed key refused", "Die Schlüssel eines unbekannten Hosts gezeigt und erfragt; ein geänderter abgelehnt"),
                         T("Copy, move, drag and drop, editor, Markdown and Properties as in the other panes", "Kopieren, Verschieben, Drag and Drop, Editor, Markdown und Eigenschaften wie in den anderen Bereichen"),
                     ],
                     "captions": [
                         T("SSH puts a remote system into the left pane: one of your hosts, a host of your SSH configuration, or an address you type.",
                           "SSH holt ein entferntes System in den linken Bereich: einen Ihrer Hosts, einen Host Ihrer SSH-Konfiguration oder eine Adresse, die Sie tippen."),
                         T("The app speaks SFTP through your OpenSSH, with your keys only: no shell runs there, no password is kept. An unknown host key is asked about, a changed one refused.",
                           "Die App spricht SFTP über Ihr OpenSSH, nur mit Ihren Schlüsseln: Dort läuft keine Shell, kein Passwort wird gemerkt. Ein unbekannter Host-Key wird erfragt, ein geänderter abgelehnt."),
                     ]},
                    {"kind": "bullets", "heading": T("Safe by design", "Sicher gebaut"),
                     "bullets": [
                         T("Symbolic links are shown, never followed or copied", "Symbolische Links werden gezeigt, nie verfolgt oder kopiert"),
                         T("Copies stop at 1 GiB, run in the background, can be cancelled", "Kopien enden bei 1 GiB, laufen im Hintergrund, lassen sich abbrechen"),
                         T("Files copied to the host lose their executable bits", "Auf den Host kopierte Dateien verlieren ihre Ausführungsrechte"),
                         T("`.git/config`, hooks, `.envrc`: a warning after the copy", "`.git/config`, Hooks, `.envrc`: eine Warnung nach dem Kopieren"),
                     ],
                     "captions": [
                         T("Symbolic links are shown with their target but never followed. A file gets its name only when its copy is complete, so nothing half copied is left.",
                           "Symbolische Links werden mit ihrem Ziel gezeigt, aber nie verfolgt. Eine Datei bekommt ihren Namen erst, wenn ihre Kopie fertig ist, also bleibt nichts halb Kopiertes zurück."),
                         T("What the agent wrote counts as untrusted: copied out, files lose their executable bits, and files that tools run, such as Git hooks, come with a warning.",
                           "Was der Agent schrieb, gilt als nicht vertrauenswürdig: Herauskopiert verlieren Dateien ihre Ausführungsrechte, und Dateien, die Werkzeuge ausführen, etwa Git-Hooks, kommen mit einer Warnung."),
                     ]},
                ],
            },
            {
                "id": "kits", "page": "kits",
                "topics": [T("Kits: what every sandbox brings", "Kits: was jede Sandbox mitbringt"),
                           T("Marketplaces and plugins", "Marktplätze und Plugins"), T("Hooks after your review", "Hooks nach Ihrer Prüfung")],
                "intro": [T("Kits give every sandbox the agent's skills, agents, commands and plugins, without exposing your own configuration.",
                            "Kits geben jeder Sandbox die Skills, Agents, Commands und Plugins des Agenten, ohne Ihre eigene Konfiguration freizulegen.")],
                "scenes": [
                    {"kind": "app", "page": "kits", "heading": T("Every kit", "Jedes Kit"),
                     "zoom": [0.14, 0.1, 1.0, 0.8],
                     "bullets": [
                         T("sandbox-ai's own kit and the default kit: in every sandbox", "Das eigene Kit von sandbox-ai und das Standard-Kit: in jeder Sandbox"),
                         T("Per agent: skills, agents, commands, prompts, memory template, plugins", "Je Agent: Skills, Agents, Commands, Prompts, Memory-Vorlage, Plugins"),
                         T("New kit…, Add to the kit, Use in the current project…", "Neues Kit…, Zum Kit hinzufügen, Im aktuellen Projekt nutzen…"),
                     ],
                     "captions": [
                         T("The list shows every kit: sandbox-ai's own and the default kit, which every sandbox gets, built-in kits and yours, with what each brings per agent.",
                           "Die Liste zeigt jedes Kit: das eigene von sandbox-ai und das Standard-Kit, die jede Sandbox bekommt, eingebaute Kits und Ihre, mit dem, was jedes je Agent mitbringt."),
                         T("New kit, skills, plugins and hooks are added with buttons above the list; Use in the current project adds the kit to the project file through the board.",
                           "Neues Kit, Skills, Plugins und Hooks fügen Sie mit den Knöpfen über der Liste hinzu; Im aktuellen Projekt nutzen trägt das Kit über das Board in die Projektdatei ein."),
                     ]},
                    {"kind": "app", "page": "kits", "shot": "kits-marketplaces", "heading": T("Marketplaces", "Marktplätze"),
                     "zoom": [0.14, 0.1, 1.0, 0.8],
                     "bullets": [
                         T("sandbox-ai's, the kits', Claude's and Codex's marketplaces", "Die Marktplätze von sandbox-ai, der Kits, von Claude und von Codex"),
                         T("Each plugin with its state: in which kits, on or off, installed", "Jedes Plugin mit Zustand: in welchen Kits, an oder aus, installiert"),
                         T("A kit declares plugins; Install the kits' plugins downloads them", "Ein Kit gibt Plugins an; Plugins der Kits installieren lädt sie"),
                     ],
                     "captions": [
                         T("The Marketplaces tab shows each marketplace and its plugins, with their state. Adding a plugin to a kit downloads nothing: the kit only declares it.",
                           "Der Reiter Marktplätze zeigt jeden Marktplatz und seine Plugins mit ihrem Zustand. Ein Plugin zu einem Kit hinzuzufügen lädt nichts: Das Kit gibt es nur an."),
                         T("Install the kits' plugins downloads them once into the plugin store, in a sandbox of its own and in the background; the log window shows its output.",
                           "Plugins der Kits installieren lädt sie einmal in den Plugin-Speicher, in einer eigenen Sandbox und im Hintergrund; das Protokollfenster zeigt die Ausgabe."),
                     ]},
                    {"kind": "bullets", "heading": T("Hooks and plugins run code", "Hooks und Plugins führen Code aus"),
                     "bullets": [
                         T("Review and approve hooks… opens a terminal: read every file, then confirm", "Hooks prüfen und freigeben… öffnet ein Terminal: jede Datei lesen, dann bestätigen"),
                         T("The app never approves a hook on its own", "Die App gibt nie selbst einen Hook frei"),
                         T("A changed hook stops running until approved again", "Ein geänderter Hook läuft erst nach neuer Freigabe wieder"),
                     ],
                     "captions": [
                         T("A hook runs code at the agent's events, and a plugin runs code in every sandbox that enables it. Approve and install only what you trust.",
                           "Ein Hook führt bei den Ereignissen des Agenten Code aus, und ein Plugin führt Code in jeder Sandbox aus, die es einschaltet. Geben Sie nur frei und installieren Sie nur, was Sie für vertrauenswürdig halten."),
                     ]},
                ],
            },
            {
                "id": "mcp", "page": "mcp",
                "topics": [T("Your servers and their state", "Ihre Server und ihr Zustand"), T("Signed catalogs", "Signierte Kataloge"),
                           T("Network only where needed", "Netz nur, wo nötig")],
                "intro": [T("MCP servers give the agent tools, such as a web search or a binary analysis. Each runs in a sandbox of its own.",
                            "MCP-Server geben dem Agenten Werkzeuge, etwa eine Websuche oder eine Binäranalyse. Jeder läuft in einer eigenen Sandbox.")],
                "scenes": [
                    {"kind": "app", "page": "mcp", "heading": T("Your servers", "Ihre Server"),
                     "zoom": [0.14, 0.08, 1.0, 0.47],
                     "bullets": [
                         T("Each with its status; select it to see its definition", "Jeder mit Status; auswählen zeigt seine Definition"),
                         T("Start or stop it; a start uses it with `--mcp NAME`", "Starten oder stoppen; ein Start nutzt ihn mit `--mcp NAME`"),
                         T("Agents reach it through a relay that applies your rules", "Agenten erreichen ihn über ein Relay, das Ihre Regeln anwendet"),
                     ],
                     "captions": [
                         T("At the top are your servers with their state. Each runs in a sandbox or container of its own, with only the network and folders its definition names.",
                           "Oben stehen Ihre Server mit ihrem Zustand. Jeder läuft in einer eigenen Sandbox oder einem eigenen Container, nur mit dem Netz und den Ordnern, die seine Definition nennt."),
                         T("The agent reaches a server through a relay that applies your rules and logs every call; there is no network path between the sandboxes.",
                           "Der Agent erreicht einen Server über ein Relay, das Ihre Regeln anwendet und jeden Aufruf protokolliert; zwischen den Sandboxen gibt es keinen Netzweg."),
                     ]},
                    {"kind": "app", "page": "mcp", "heading": T("The catalogs", "Die Kataloge"),
                     "zoom": [0.14, 0.46, 1.0, 0.88],
                     "bullets": [
                         T("sandbox-ai-mcp and axross-mcp-hub, signed", "sandbox-ai-mcp und axross-mcp-hub, signiert"),
                         T("Every server reviewed in full, pinned by digest", "Jeder Server vollständig geprüft, per Digest festgelegt"),
                         T("Add… asks for folders, variables, network and limits", "Hinzufügen… fragt nach Ordnern, Variablen, Netz und Grenzen"),
                         T("Unsigned only when you say so", "Unsigniert nur, wenn Sie es sagen"),
                     ],
                     "captions": [
                         T("Below are the catalogs: ready-made servers, each reviewed in full, pinned by digest and signed. The column Signature says whether it was verified.",
                           "Darunter stehen die Kataloge: fertige Server, jeder vollständig geprüft, per Digest festgelegt und signiert. Die Spalte Signatur sagt, ob sie geprüft wurde."),
                         T("Give a server network only where you would give it to the agent: a fetch or browser server sends whatever the agent asks it to.",
                           "Geben Sie einem Server nur dort Netz, wo Sie es dem Agenten geben würden: Ein Fetch- oder Browser-Server sendet, was immer der Agent von ihm verlangt."),
                         T("A start uses a server by name, or you connect it to the agent on the board; Add MCP server on the Sandboxes page gives it to a running sandbox without a restart.",
                           "Ein Start nutzt einen Server per Namen, oder Sie verbinden ihn auf dem Board mit dem Agenten; MCP-Server hinzufügen auf der Seite Sandboxen gibt ihn einer laufenden Sandbox ohne Neustart."),
                     ]},
                    {"kind": "app", "page": "mcp", "shot": "mcp-properties", "heading": T("Properties and limits", "Eigenschaften und Grenzen"),
                     "zoom": [0.37, 0.25, 0.63, 0.74],
                     "bullets": [
                         T("Properties… (double click, Alt+Enter, right click): limits, network, folders, VPN", "Eigenschaften… (Doppelklick, Alt+Enter, Rechtsklick): Grenzen, Netz, Ordner, VPN"),
                         T("Memory, CPUs, processes and /tmp, checked while typed", "Speicher, CPUs, Prozesse und /tmp, beim Tippen geprüft"),
                         T("Use the recommended limits: what the catalog recommends", "Die empfohlenen Grenzen nehmen: was der Katalog empfiehlt"),
                         T("What ended a server is recorded: out of memory, its process limit, a full /tmp", "Was einen Server beendete, wird festgehalten: kein Speicher, Prozessgrenze, volles /tmp"),
                     ],
                     "captions": [
                         T("Properties shows a server's own limits, network and folders, and checks every value while you type. A start that needs more gets an instance of its own.",
                           "Eigenschaften zeigt die eigenen Grenzen, das Netz und die Ordner eines Servers und prüft jeden Wert beim Tippen. Ein Start, der mehr braucht, bekommt eine eigene Instanz."),
                         T("When a server ends without being asked to, for lack of memory or a full temporary folder, the page says why and what fixes it.",
                           "Endet ein Server ungefragt, aus Mangel an Speicher oder mit vollem temporärem Ordner, sagt die Seite, warum, und was es behebt."),
                     ]},
                ],
            },
            {
                "id": "llm", "page": "llm",
                "topics": [T("Your own model servers", "Ihre eigenen Modellserver"), T("What they have loaded", "Was sie geladen haben"),
                           T("Keys stay in the proxy", "Schlüssel bleiben im Proxy")],
                "intro": [T("LLM servers lists the model servers an agent can use instead of the cloud: Ollama, LM Studio, llama.cpp, vLLM or another compatible server.",
                            "LLM-Server listet die Modellserver, die ein Agent statt der Cloud nutzen kann: Ollama, LM Studio, llama.cpp, vLLM oder ein anderer kompatibler Server.")],
                "scenes": [
                    {"kind": "app", "page": "llm", "heading": T("Every server by name", "Jeder Server mit Namen"),
                     "zoom": [0.14, 0.08, 1.0, 0.36],
                     "bullets": [
                         T("Name, kind, URL, default model, key, VPN profile", "Name, Art, URL, Standardmodell, Schlüssel, VPN-Profil"),
                         T("What it has loaded now: models, VRAM and RAM, requests", "Was er gerade geladen hat: Modelle, VRAM und RAM, Anfragen"),
                         T("On this machine or a host of `sandbox-ai hosts`", "Auf diesem Rechner oder einem Host aus `sandbox-ai hosts`"),
                     ],
                     "captions": [
                         T("The table shows each server with its kind, address and default model. Below it, a line per server says what it has loaded now and what it uses.",
                           "Die Tabelle zeigt jeden Server mit Art, Adresse und Standardmodell. Darunter sagt eine Zeile je Server, was er gerade geladen hat und was er verbraucht."),
                         T("What a server's API does not report is said, never guessed.",
                           "Was die API eines Servers nicht meldet, wird gesagt, nie geraten."),
                     ]},
                    {"kind": "bullets", "heading": T("Add…, Check, Models", "Hinzufügen…, Prüfen, Modelle"),
                     "bullets": [
                         T("Add… checks the server first, then keeps the key as a secret", "Hinzufügen… prüft den Server zuerst, dann wird der Schlüssel als Geheimnis gespeichert"),
                         T("Test connection lists the models, loaded first", "Verbindung testen listet die Modelle, geladene zuerst"),
                         T("Check: reachability, TLS, key, model, the API the agent needs", "Prüfen: Erreichbarkeit, TLS, Schlüssel, Modell, die API, die der Agent braucht"),
                         T("A start: `--llm NAME:MODEL`, or an LLM part on the board", "Ein Start: `--llm NAME:MODEL`, oder ein LLM-Teil auf dem Board"),
                     ],
                     "captions": [
                         T("Add registers a server: it is checked first, and only then is a key you type kept as a secret. Check tests reachability, TLS, the key and the API the agent needs.",
                           "Hinzufügen registriert einen Server: Er wird zuerst geprüft, und erst dann wird ein eingegebener Schlüssel als Secret gespeichert. Prüfen testet Erreichbarkeit, TLS, den Schlüssel und die API, die der Agent braucht."),
                         T("A start opens only the server's address and port, and the proxy adds the key on the wire: the agent only ever sees a placeholder.",
                           "Ein Start öffnet nur Adresse und Port des Servers, und der Proxy setzt den Schlüssel unterwegs ein: Der Agent sieht nur einen Platzhalter."),
                         T("A server can sit behind a VPN profile, and on another host the list is that host's own, read over SSH.",
                           "Ein Server kann hinter einem VPN-Profil liegen, und auf einem anderen Host ist die Liste die eigene dieses Hosts, über SSH gelesen."),
                     ]},
                    {"kind": "app", "page": "llm", "shot": "llm-parameters", "heading": T("Parameters and the loop guard", "Parameter und Loop Guard"),
                     "zoom": [0.33, 0.01, 0.67, 0.99],
                     "bullets": [
                         T("Sampling parameters per model: temperature, top_k, context size", "Sampling-Parameter je Modell: Temperatur, top_k, Kontextgröße"),
                         T("Set by the proxy in that server's requests, for every agent", "Vom Proxy in die Anfragen an diesen Server gesetzt, für jeden Agenten"),
                         T("Loop guard: a note, a pause of tool use, a clean end of a repeating answer", "Loop Guard: ein Hinweis, eine Pause der Tools, ein sauberes Ende einer sich wiederholenden Antwort"),
                     ],
                     "captions": [
                         T("Parameters sets a model's sampling, such as temperature and context size. The proxy puts them into that server's requests, whichever agent asks.",
                           "Parameter setzt das Sampling eines Modells, etwa Temperatur und Kontextgröße. Der Proxy setzt sie in die Anfragen an diesen Server, egal welcher Agent fragt."),
                         T("The loop guard keeps a local model from going round in circles: it adds a note, pauses tool use for one answer, or ends an answer that repeats itself.",
                           "Der Loop Guard bewahrt ein lokales Modell davor, sich im Kreis zu drehen: Er fügt einen Hinweis an, pausiert die Tools für eine Antwort oder beendet eine Antwort, die sich wiederholt."),
                     ]},
                ],
            },
            {
                "id": "images", "page": "images",
                "topics": [T("The agents' images and variants", "Die Images der Agenten und Varianten"),
                           T("Build", "Bauen"), T("The extension editor", "Der Erweiterungs-Editor")],
                "intro": [T("Images lists the container image of each agent and your variants: built or not, current or due for a rebuild.",
                            "Images listet das Container-Image jedes Agenten und Ihre Varianten: gebaut oder nicht, aktuell oder fällig für einen Neubau.")],
                "scenes": [
                    {"kind": "app", "page": "images", "heading": T("Images and variants", "Images und Varianten"),
                     "zoom": [0.14, 0.08, 1.0, 0.5],
                     "bullets": [
                         T("Built, current with its extension, agent version, size", "Gebaut, aktuell mit seiner Erweiterung, Agent-Version, Größe"),
                         T("The installed packages of the selected image", "Die installierten Pakete des gewählten Images"),
                         T("Build; Other builds: the extension layer only, or all images", "Bauen; Weitere Builds: nur die Erweiterungsschicht oder alle Images"),
                         T("New variant…: a copy with its own extension, `--image-variant`", "Neue Variante…: eine Kopie mit eigener Erweiterung, `--image-variant`"),
                     ],
                     "captions": [
                         T("Each row says whether the image is built and current, or needs a rebuild for a changed extension or a new base. Build runs sandbox-ai build for it.",
                           "Jede Zeile sagt, ob das Image gebaut und aktuell ist oder einen Neubau braucht, für eine geänderte Erweiterung oder eine neue Basis. Bauen führt dafür sandbox-ai build aus."),
                         T("A variant is a copy of an agent's image with its own extension, for one kind of work; a start chooses it by name.",
                           "Eine Variante ist eine Kopie des Images eines Agenten mit eigener Erweiterung, für eine Art von Arbeit; ein Start wählt sie per Namen."),
                     ]},
                    {"kind": "app", "page": "images", "shot": "images-yours", "heading": T("Your images", "Eigene Images"),
                     "zoom": [0.14, 0.48, 1.0, 0.92],
                     "bullets": [
                         T("Pull and pin: an image from a registry, pinned by its digest", "Holen und pinnen: ein Image aus einer Registry, per Digest gepinnt"),
                         T("Add from a Containerfile…: every FROM pulled once and pinned", "Aus einem Containerfile hinzufügen…: jedes FROM einmal geholt und gepinnt"),
                         T("Marked UNCHECKED wherever it appears; no start ever pulls one", "Überall als UNGEPRÜFT markiert; kein Start holt je eines"),
                         T("Used as a service beside the agent, or as the base of a variant", "Genutzt als Service neben dem Agenten oder als Basis einer Variante"),
                     ],
                     "captions": [
                         T("Your images comes from a registry or a Containerfile of yours, pulled once and pinned by its digest. Nobody has reviewed them, so they are marked unchecked everywhere.",
                           "Eigene Images kommen aus einer Registry oder aus Ihrem Containerfile, einmal geholt und per Digest gepinnt. Niemand hat sie geprüft, deshalb sind sie überall als ungeprüft markiert."),
                         T("Such an image runs as a service beside the agent, or becomes the base of a variant.",
                           "So ein Image läuft als Service neben dem Agenten oder wird zur Basis einer Variante."),
                     ]},
                    {"kind": "app", "page": "images", "shot": "images-extension", "heading": T("The extension editor", "Der Erweiterungs-Editor"),
                     "zoom": [0.14, 0.47, 1.0, 0.97],
                     "bullets": [
                         T("Debian and npm packages, Python tools, files", "Debian- und npm-Pakete, Python-Werkzeuge, Dateien"),
                         T("A Containerfile fragment with the validator's findings in place", "Ein Containerfile-Fragment mit den Befunden des Validators an Ort und Stelle"),
                         T("Save runs `sandbox-ai images extend`, refused while there are errors", "Speichern führt `sandbox-ai images extend` aus, abgelehnt, solange es Fehler gibt"),
                     ],
                     "captions": [
                         T("The extension editor has a tab for Debian packages, npm packages, Python tools, files and a Containerfile fragment, with a preview of the final Containerfile.",
                           "Der Erweiterungs-Editor hat Reiter für Debian-Pakete, npm-Pakete, Python-Werkzeuge, Dateien und ein Containerfile-Fragment, mit einer Vorschau des fertigen Containerfiles."),
                         T("Save is refused while the validator finds errors, and it changes only the extension: the image changes when you build it again.",
                           "Speichern wird abgelehnt, solange der Validator Fehler findet, und ändert nur die Erweiterung: Das Image ändert sich, wenn Sie es neu bauen."),
                         T("The app never writes an extension itself: Save runs sandbox-ai images extend, and the build runs in a terminal you can watch.",
                           "Die App schreibt nie selbst eine Erweiterung: Speichern führt sandbox-ai images extend aus, und der Build läuft in einem Terminal, dem Sie zusehen können."),
                     ]},
                ],
            },
            {
                "id": "credentials", "page": "credentials",
                "topics": [T("Agent logins", "Logins der Agenten"), T("Secrets and Git logins", "Secrets und Git-Logins"),
                           T("Identities and VPN profiles", "Identitäten und VPN-Profile")],
                "intro": [T("Credentials keeps what sandboxes may log in with. A value kept here is never read back or shown.",
                            "Zugangsdaten verwaltet, womit sich Sandboxen anmelden dürfen. Ein hier gespeicherter Wert wird nie zurückgelesen oder angezeigt.")],
                "scenes": [
                    {"kind": "app", "page": "credentials", "heading": T("The overview", "Die Übersicht"),
                     "zoom": [0.14, 0.1, 1.0, 0.6],
                     "bullets": [
                         T("Every agent's login, valid or expired; Log in… in a terminal", "Der Login jedes Agenten, gültig oder abgelaufen; Anmelden… im Terminal"),
                         T("A sandbox gets the login when it starts", "Eine Sandbox bekommt den Login beim Start"),
                         T("Tabs for secrets, Git and publish logins, identities, VPN", "Reiter für Geheimnisse, Git- und Veröffentlichungs-Logins, Identitäten, VPN"),
                         T("Every tab laid out alike: nothing moves when you switch", "Jeder Reiter gleich aufgebaut: Beim Wechseln verschiebt sich nichts"),
                     ],
                     "captions": [
                         T("The overview shows every agent's login, whether it is valid, and where it comes from. Logging in runs in a terminal; a sandbox gets the login when it starts.",
                           "Die Übersicht zeigt den Login jedes Agenten, ob er gültig ist und woher er kommt. Die Anmeldung läuft in einem Terminal; eine Sandbox bekommt den Login beim Start."),
                         T("Every tab has the same layout: a header that says what it is for, the list, the buttons and a message area, so nothing moves when you switch tabs.",
                           "Jeder Reiter hat denselben Aufbau: eine Kopfzeile, die sagt, wofür er ist, die Liste, die Knöpfe und einen Bereich für Meldungen, damit sich beim Wechseln nichts verschiebt."),
                     ]},
                    {"kind": "app", "page": "credentials", "shot": "credentials-secrets", "heading": T("Secrets", "Secrets"),
                     "zoom": [0.14, 0.1, 1.0, 0.5],
                     "bullets": [
                         T("API keys and tokens by name, in your keyring or private files", "API-Schlüssel und Tokens mit Namen, im Schlüsselbund oder in privaten Dateien"),
                         T("Passed on standard input, never as an argument", "Über die Standardeingabe übergeben, nie als Argument"),
                         T("The agent sees a placeholder; the proxy adds the token on the wire", "Der Agent sieht einen Platzhalter; der Proxy setzt den Token unterwegs ein"),
                     ],
                     "captions": [
                         T("Secrets are API keys and tokens, kept by name in your keyring. A value is passed on standard input, never on a command line, and never shown again.",
                           "Secrets sind API-Schlüssel und Tokens, mit Namen in Ihrem Schlüsselbund gespeichert. Ein Wert geht über die Standardeingabe, nie über eine Kommandozeile, und wird nie wieder angezeigt."),
                         T("With an API key, Git logins or the token proxy, the token never enters the sandbox: the proxy adds it on the wire, and the agent sees a placeholder.",
                           "Mit API-Key, Git-Logins oder dem Token-Proxy kommt der Token nie in die Sandbox: Der Proxy setzt ihn unterwegs ein, und der Agent sieht einen Platzhalter."),
                     ]},
                    {"kind": "app", "page": "credentials", "shot": "credentials-give", "heading": T("Give to a running sandbox", "An eine laufende Sandbox übergeben"),
                     "zoom": [0.14, 0.12, 1.0, 0.86],
                     "bullets": [
                         T("A secret as an API key for one host, without a restart", "Ein Secret als API-Schlüssel für einen Host, ohne Neustart"),
                         T("For a Git server: As a Git login (for git push)", "Für einen Git-Server: Als Git-Login (für git push)"),
                         T("`sandbox-ai secrets give RUN NAME --api-key HOST`", "`sandbox-ai secrets give LAUF NAME --api-key HOST`"),
                         T("`sandbox-ai git-logins give RUN URL --secret NAME`", "`sandbox-ai git-logins give LAUF URL --secret NAME`"),
                     ],
                     "captions": [
                         T("Give to a running sandbox hands a secret to a sandbox that already runs, as an API key for one server: the proxy adds it there, and nowhere else.",
                           "An laufende Sandbox übergeben gibt ein Secret einer Sandbox, die schon läuft, als API-Schlüssel für einen Server: Der Proxy setzt ihn dort ein, und nirgends sonst."),
                         T("When the server is a Git server, such as GitHub, the dialog offers the secret as a Git login, so that git push works there.",
                           "Ist der Server ein Git-Server, etwa GitHub, bietet der Dialog das Secret als Git-Login an, damit git push dort funktioniert."),
                         T("The same works in a terminal, with the commands secrets give and git-logins give; revoke takes it back, and nothing is left half applied.",
                           "Dasselbe geht im Terminal, mit den Befehlen secrets give und git-logins give; revoke nimmt es zurück, und nichts bleibt halb angewendet."),
                     ]},
                    {"kind": "app", "page": "credentials", "shot": "credentials-vpn", "heading": T("Identities and VPN profiles", "Identitäten und VPN-Profile"),
                     "bullets": [
                         T("Identities: commit name and address, Git and publish logins", "Identitäten: Commit-Name und Adresse, Git- und Veröffentlichungs-Logins"),
                         T("VPN: WireGuard, OpenVPN, Mullvad, Tailscale or Headscale", "VPN: WireGuard, OpenVPN, Mullvad, Tailscale oder Headscale"),
                         T("Keys and passwords become secrets; the profile only names them", "Schlüssel und Passwörter werden Geheimnisse; das Profil nennt nur ihre Namen"),
                     ],
                     "captions": [
                         T("An identity says who a sandbox works as: the name and address of its commits and its logins. A VPN profile puts a tunnel in front of a sandbox.",
                           "Eine Identität sagt, als wer eine Sandbox arbeitet: Name und Adresse ihrer Commits und ihre Logins. Ein VPN-Profil setzt einen Tunnel vor eine Sandbox."),
                         T("Keys and passwords of a VPN become secrets too; the profile only names them.",
                           "Auch Schlüssel und Passwörter eines VPN werden Secrets; das Profil nennt nur ihre Namen."),
                     ]},
                ],
            },
            {
                "id": "team", "page": "team",
                "topics": [T("Two or three agents, one workspace", "Zwei oder drei Agenten, ein Workspace"),
                           T("The lead and the task board", "Die Leitung und das Aufgaben-Board"), T("Start, talk, stop", "Starten, sprechen, stoppen")],
                "intro": [T("Teams lists the teams of agents: two or three agents, each in a sandbox of its own, on one shared workspace.",
                            "Teams listet die Agenten-Teams: zwei oder drei Agenten, jeder in einer eigenen Sandbox, auf einem gemeinsamen Workspace.")],
                "scenes": [
                    {"kind": "diagram", "diagram": "team", "heading": T("A team works through a task board", "Ein Team arbeitet über ein Aufgaben-Board"),
                     "captions": [
                         T("The first agent leads: you talk to it, and it hands the others tasks on the team's board, an MCP server in a sandbox without network.",
                           "Der erste Agent leitet: Mit ihm sprechen Sie, und er gibt den anderen Aufgaben über das Aufgaben-Board des Teams, einen MCP-Server in einer Sandbox ohne Netz."),
                     ]},
                    {"kind": "app", "page": "team", "heading": T("The Teams page", "Die Seite Teams"),
                     "bullets": [
                         T("Start a team…: name, lead, members, workspace, options", "Team starten…: Name, Leitung, Mitglieder, Workspace, Optionen"),
                         T("Members: sessions NAME-AGENT, with their state, and the board", "Mitglieder: Sessions NAME-AGENT mit Zustand, und das Aufgaben-Board"),
                         T("Talk to the lead; Post a task… for one member", "Mit der Leitung sprechen; Aufgabe stellen… für ein Mitglied"),
                         T("Stop… and Delete… ask first; the workspace stays", "Stoppen… und Löschen… fragen zuerst; der Workspace bleibt"),
                     ],
                     "captions": [
                         T("Start a team asks for a name, the lead, the members, the workspace and the options every member starts with, then starts them one after the other.",
                           "Team starten fragt nach Name, Leitung, Mitgliedern, Workspace und den Optionen, mit denen jedes Mitglied startet, und startet sie dann nacheinander."),
                         T("Select a team to see its members and its board. Talk to the lead attaches to the lead's session; Post a task gives one member a task from you.",
                           "Wählen Sie ein Team, um seine Mitglieder und sein Aufgaben-Board zu sehen. Mit der Leitung sprechen verbindet mit der Session der Leitung; Aufgabe stellen gibt einem Mitglied eine Aufgabe von Ihnen."),
                         T("Members share the workspace, so nothing stops two of them from editing one file: plan the work accordingly.",
                           "Die Mitglieder teilen den Workspace, nichts hindert also zwei daran, dieselbe Datei zu ändern: Planen Sie die Arbeit entsprechend."),
                         T("Stop ends every member and the board; Delete also removes their HOMEs and the board for good. Both ask first, and the workspace stays.",
                           "Stoppen beendet jedes Mitglied und das Aufgaben-Board; Löschen entfernt auch ihre HOMEs und das Board endgültig. Beide fragen zuerst, und der Workspace bleibt."),
                     ]},
                ],
            },
            {
                "id": "system", "page": "system",
                "topics": [T("Version, updates, policy", "Version, Updates, Richtlinie"), T("The host check", "Die Host-Prüfung"),
                           T("Settings and the log window", "Einstellungen und das Protokollfenster")],
                "intro": [T("System shows the sandbox-ai in use and keeps this machine in order.",
                            "System zeigt das verwendete sandbox-ai und hält diesen Rechner in Ordnung.")],
                "scenes": [
                    {"kind": "app", "page": "system", "heading": T("sandbox-ai and the host check", "sandbox-ai und die Host-Prüfung"),
                     "bullets": [
                         T("Version and folder; Check for updates", "Version und Ordner; Auf Updates prüfen"),
                         T("The administrator's policy, in one line", "Die Richtlinie des Administrators, in einer Zeile"),
                         T("Host check: what is wrong and how to fix it", "Host-Prüfung: was fehlt und wie man es behebt"),
                         T("Build images, update, privacy check of a repository", "Images bauen, aktualisieren, Datenschutzprüfung eines Repositorys"),
                     ],
                     "captions": [
                         T("At the top are the version and the administrator's policy. The host check runs sandbox-ai doctor: each row says what is wrong and how to fix it.",
                           "Oben stehen die Version und die Richtlinie des Administrators. Die Host-Prüfung führt sandbox-ai doctor aus: Jede Zeile sagt, was fehlt und wie man es behebt."),
                         T("Run it after installing, and fix the first red row first.",
                           "Führen Sie sie nach der Installation aus und beheben Sie die erste rote Zeile zuerst."),
                         T("Below it are the buttons to build the images, to update sandbox-ai and to check a repository for private data before it is published.",
                           "Darunter stehen die Knöpfe, um die Images zu bauen, sandbox-ai zu aktualisieren und ein Repository vor der Veröffentlichung auf private Daten zu prüfen."),
                     ]},
                    {"kind": "app", "page": "system", "shot": "system-settings", "heading": T("Settings", "Einstellungen"),
                     "zoom": [0.19, 0.04, 0.81, 0.96],
                     "captions": [
                         T("Settings choose the theme, the language, where builds and logins run, and whether the app keeps answering questions from the tray when its window is closed.",
                           "Die Einstellungen wählen Farbschema, Sprache, wo Builds und Anmeldungen laufen, und ob die App aus dem Infobereich weiter Fragen beantwortet, wenn ihr Fenster zu ist."),
                         T("Pop-up notifications are off by default and switched on here; the loop guard for local models has its section here too.",
                           "Benachrichtigungen sind anfangs aus und werden hier eingeschaltet; auch der Loop Guard für lokale Modelle hat hier seinen Abschnitt."),
                         T("Here, too, you choose whether the app reopens the terminals of running sessions when it starts; that is on by default.",
                           "Hier wählen Sie auch, ob die App beim Start die Terminals laufender Sessions wieder öffnet; das ist anfangs an."),
                     ]},
                    {"kind": "app", "page": "system", "shot": "system-log", "heading": T("The log in a window of its own", "Das Protokoll in einem eigenen Fenster"),
                     "zoom": [0.15, 0.17, 0.93, 0.82],
                     "captions": [
                         T("The app's log has a window of its own, Control Shift L, also from inside a dialog: what a dialog reports can be read and copied while it is open.",
                           "Das Protokoll der App hat ein eigenes Fenster, Strg Umschalt L, auch aus einem Dialog heraus: Was ein Dialog meldet, lässt sich lesen und kopieren, während er offen ist."),
                         T("No dialog blocks the app: the page it came from waits behind a veil, and the rest of the app stays usable.",
                           "Kein Dialog blockiert die App: Die Seite, von der er kam, wartet hinter einem Schleier, und der Rest der App bleibt benutzbar."),
                         T("Every extra window, a dialog, the log or an editor, has a cross at its top right and closes with Escape, also where the window manager draws no title bar.",
                           "Jedes weitere Fenster, ein Dialog, das Protokoll oder ein Editor, hat oben rechts ein Kreuz und schließt sich mit Esc, auch wo der Fenstermanager keine Titelleiste zeichnet."),
                     ]},
                ],
            },
            {
                "id": "hosts", "page": "hosts",
                "topics": [T("Other machines over SSH", "Andere Rechner über SSH"), T("Updates", "Updates"),
                           T("Each host's defaults", "Die Vorgaben jedes Hosts")],
                "intro": [T("Hosts manages other machines, such as Proxmox containers or servers, whose sandbox-ai this app controls over SSH.",
                            "Hosts verwaltet andere Rechner, etwa Proxmox-Container oder Server, deren sandbox-ai diese App über SSH steuert.")],
                "scenes": [
                    {"kind": "app", "page": "hosts", "heading": T("One row per host", "Eine Zeile je Host"),
                     "zoom": [0.14, 0.07, 0.55, 0.75],
                     "bullets": [
                         T("SSH address, whether it answered, version, starts, questions", "SSH-Adresse, ob er antwortete, Version, Starts, Fragen"),
                         T("Show starts, Show questions: the other pages, for this host", "Starts zeigen, Fragen zeigen: die anderen Seiten, für diesen Host"),
                         T("Add…, Test: SSH, the host's sandbox-ai and its doctor", "Hinzufügen…, Testen: SSH, das sandbox-ai des Hosts und sein doctor"),
                     ],
                     "captions": [
                         T("Each row is a host: its address, whether it answered, its sandbox-ai version, how many starts run there and how many questions wait.",
                           "Jede Zeile ist ein Host: seine Adresse, ob er geantwortet hat, seine sandbox-ai-Version, wie viele Starts dort laufen und wie viele Fragen warten."),
                         T("Its starts and questions are on the pages Sandboxes and Approvals, as for this computer. SSH uses your keys, and the host key is checked strictly after the first contact.",
                           "Seine Starts und Fragen stehen auf den Seiten Sandboxen und Freigaben, wie für diesen Rechner. SSH nutzt Ihre Schlüssel, und der Host-Key wird nach dem ersten Kontakt streng geprüft."),
                     ]},
                    {"kind": "app", "page": "hosts", "shot": "hosts-defaults", "heading": T("Updates and defaults", "Updates und Vorgaben"),
                     "zoom": [0.53, 0.07, 1.0, 0.6],
                     "bullets": [
                         T("Update, Update all: each host's own signed update", "Aktualisieren, Alle aktualisieren: das eigene signierte Update jedes Hosts"),
                         T("Update from here…: this machine's sandbox-ai, for a host without the internet", "Von hier aktualisieren…: das sandbox-ai dieses Rechners, für einen Host ohne Internet"),
                         T("Defaults: security mode, canaries, keep_days, telemetry", "Vorgaben: Sicherheitsmodus, Canaries, keep_days, Telemetrie"),
                     ],
                     "captions": [
                         T("Update runs each host's own signed update. Update from here puts this machine's sandbox-ai on a host that cannot reach the release server.",
                           "Aktualisieren führt das eigene signierte Update jedes Hosts aus. Von hier aktualisieren bringt das sandbox-ai dieses Rechners auf einen Host, der den Release-Server nicht erreicht."),
                         T("The tabs on the right are about the chosen host: its doctor, its updates and its defaults. A step down in security is asked first.",
                           "Die Reiter rechts betreffen den gewählten Host: seinen doctor, seine Updates und seine Vorgaben. Ein Schritt nach unten bei der Sicherheit wird zuerst erfragt."),
                         T("Every action runs sandbox-ai on the host over SSH, each argument quoted on its own; nothing listens on the hosts but their SSH server.",
                           "Jede Aktion führt sandbox-ai auf dem Host über SSH aus, jedes Argument einzeln gequotet; auf den Hosts lauscht nichts außer ihrem SSH-Server."),
                     ]},
                ],
            },
            {
                "id": "help", "page": "help",
                "topics": [T("Every page and concept", "Jede Seite und jedes Konzept"), T("Tours", "Touren"),
                           T("Working with the app", "Arbeiten mit der App")],
                "intro": [T("The Help page explains every page and feature of the app and the concepts of sandbox-ai, in English and German.",
                            "Die Seite Hilfe erklärt jede Seite und Funktion der App und die Konzepte von sandbox-ai, auf Deutsch und Englisch.")],
                "scenes": [
                    {"kind": "app", "page": "help", "shot": "help-section", "heading": T("Sections, search, tours", "Abschnitte, Suche, Touren"),
                     "bullets": [
                         T("Sections: the app and the concepts", "Abschnitte: die App und die Konzepte"),
                         T("Search in English and German at once", "Suche auf Deutsch und Englisch zugleich"),
                         T("Related commands and options, as they are typed", "Zugehörige Befehle und Optionen, wie man sie tippt"),
                         T("Tour about… (Ctrl+F1): tours and sections on a topic", "Tour zu… (Strg+F1): Touren und Abschnitte zu einem Thema"),
                     ],
                     "captions": [
                         T("On the left are the sections, grouped into the app and the concepts; the search keeps only sections with every word you type.",
                           "Links stehen die Abschnitte, gruppiert in die App und die Konzepte; die Suche behält nur Abschnitte mit jedem Wort, das Sie tippen."),
                         T("Buttons start the tours of a section, and Tour about asks for a topic, such as network or presets, and lists the best tours and sections first.",
                           "Knöpfe starten die Touren eines Abschnitts, und Tour zu fragt nach einem Thema, etwa Netz oder Presets, und listet die besten Touren und Abschnitte zuerst."),
                     ]},
                    {"kind": "bullets", "heading": T("Working with the app", "Arbeiten mit der App"),
                     "bullets": [
                         T("A (?) on every page, panel and part", "Ein (?) auf jeder Seite, jedem Bereich und Teil"),
                         T("Mouse side buttons and Alt+Left/Right: back and forward", "Maus-Seitentasten und Alt+Links/Rechts: zurück und vor"),
                         T("Several rows at once (Ctrl, Shift, Ctrl+A); Ctrl+C copies", "Mehrere Zeilen auf einmal (Strg, Umschalt, Strg+A); Strg+C kopiert"),
                         T("No dialog blocks the app; [x] or Esc closes every extra window; Ctrl+1 to Ctrl+9 open the pages", "Kein Dialog blockiert die App; [x] oder Esc schließt jedes weitere Fenster; Strg+1 bis Strg+9 öffnen die Seiten"),
                     ],
                     "captions": [
                         T("Every page, panel and part has a small question mark: it opens a short explanation that ends with a link to its section here.",
                           "Jede Seite, jeder Bereich und jedes Teil hat ein kleines Fragezeichen: Es öffnet eine kurze Erklärung, die mit einem Link zu ihrem Abschnitt hier endet."),
                         T("The mouse's side buttons go back and forward through the pages. In every list you select several rows at once and copy them.",
                           "Die Seitentasten der Maus gehen durch die Seiten zurück und vor. In jeder Liste wählen Sie mehrere Zeilen auf einmal und kopieren sie."),
                         T("A switch shows the help in English or German; it follows the app's language unless you change it.",
                           "Ein Schalter zeigt die Hilfe auf Deutsch oder Englisch; er folgt der Sprache der App, außer Sie ändern ihn."),
                     ]},
                ],
            },
        ],
    },
}


# The series follows the app's navigation (APP_PAGES' order), wherever a video is written above.
SERIES["pages"]["videos"].sort(key=lambda video: list(APP_PAGES).index(video["page"]))


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


def series_videos(name="pages"):
    """The videos of series NAME."""
    return SERIES[name]["videos"]


def iter_series_scenes(name="pages"):
    """(video, index, scene) for every scene of every video of series NAME; its id is VIDEO-ID/INDEX."""
    for video in series_videos(name):
        for index, scene in enumerate(video["scenes"]):
            yield video, index, scene


def narration(lang, name="pages"):
    """(where, text) of every narration of series NAME in LANG: title cards, scenes, the closing card."""
    found = []
    for video in series_videos(name):
        found += [(f"{video['id']}/intro", text(c, lang)) for c in video["intro"]]
        for index, scene in enumerate(video["scenes"]):
            found += [(f"{video['id']}/{index}", text(c, lang)) for c in scene["captions"]]
    found += [("outro", text(c, lang)) for c in SERIES[name]["outro_captions"]]
    return found


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
    for video, index, scene in iter_series_scenes():
        for value in texts_of(scene):
            for lang in LANGUAGES:
                for span in re.findall(r"`([^`]+)`", text(value, lang)):
                    if span.startswith("sandbox-ai "):
                        found.append((f"pages/{video['id']}/{index}", span))
    return found
