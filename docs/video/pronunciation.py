"""How the German narration says the English IT terms: in English, not Germanised (data only).

narrate.py reads this file. A term of LEXICON found in a narration (a whole word; hyphens split a compound, so
"MCP-Server" is "MCP" and "Server") is said with English phonemes; everything else is read by the voice in its
own language. To add a term, write it as it appears in the text (case matters) and how an English speaker would
spell it out: "Sandboxen": "sandboxes" (espeak-ng's English voice makes the phonemes, the same for every render),
or give the phonemes yourself between slashes: "Podman": "/pˈɒdmən/". Inflected forms are terms of their own.

tests/test_video_storyboard.py checks every German narration of the video series: every word that also occurs
in the English texts, every abbreviation in capitals and every word with a capital inside (GitHub) must be a term
here or be listed in GERMAN (words said the German way on purpose: German words that English happens to share,
names and IT words that are German words too). A word that starts with a term but is longer ("Sandboxwelt") is
refused: write it with a hyphen ("Sandbox-Welt").
"""

LEXICON = {
    # the product and its parts
    "sandbox-ai": "/sˈandbɒks ˌeɪˈaɪ/",  # espeak-ng reads a lone "A" as the article
    "sandbox-ai-mcp": "/sˈandbɒks ˌeɪˈaɪ ˌɛmsiːpˈiː/",
    "axross-mcp-hub": "across M C P hub",
    "Sandbox": "sandbox", "Sandboxen": "sandboxes",
    "Workspace": "workspace", "Workspaces": "workspaces",
    "Session": "session", "Sessions": "sessions",
    "Proxy": "proxy", "Proxys": "proxies",
    "Board": "board", "Boards": "boards",
    "Preset": "preset", "Presets": "presets",
    "Kill-Switch": "kill switch",
    "Supervisor": "supervisor",
    "Guide": "guide",
    "Relay": "relay",
    "Canaries": "canaries",
    "Kit": "kit", "Kits": "kits",
    "Skills": "skills", "Agents": "agents", "Commands": "commands",
    "Plugin": "plugin", "Plugins": "plugins",
    "Hook": "hook", "Hooks": "hooks",
    "Container": "container", "Containerfile": "container file",
    "Containerfiles": "container files",
    "Image": "image", "Images": "images",
    "Backend": "back end", "Engine": "engine",
    "Build": "build", "Builds": "builds",
    "Update": "update", "Updates": "updates",
    "Cloud": "cloud",
    "Host": "host", "Hosts": "hosts",
    "Login": "login", "Logins": "logins",
    "Secret": "secret", "Secrets": "secrets",
    "Token": "token", "Tokens": "tokens",
    "API": "/ˌeɪpiːˈaɪ/",
    "Prompt": "prompt", "Prompts": "prompts",
    "Shell": "shell",
    "Tab": "tab", "Tabs": "tabs",
    "Drag": "drag", "Drop": "drop",
    "Mount": "mount",
    "Lookup": "lookup",
    "Push": "push", "Pushes": "pushes", "Commits": "commits",
    "Repositorys": "repositories",
    "Header": "header", "Bodies": "bodies", "URL": "U R L",
    "Gist": "gist",
    "Digest": "digest",
    "Approval": "approval",
    "HOME": "home",
    "Setup": "set up", "Setups": "set ups",
    "Server": "server",
    # names, protocols and tools
    "MCP": "M C P", "LLM": "L L M", "VPN": "V P N",
    "Tor": "tor",
    "SSH": "S S H", "TLS": "T L S", "VRAM": "V RAM", "RAM": "RAM",
    "PDF": "P D F", "HTML": "H T M L", "Markdown": "markdown",
    "Podman": "/pˈɒdmən/", "rootless": "rootless",
    "Firejail": "fire jail", "Bubblewrap": "bubble wrap",
    "Claude": "Claude", "Claude Code": "Claude code", "Codex": "codex", "OpenCode": "open code",
    "Ollama": "/ɒlˈɑːmə/", "LM Studio": "L M studio", "llama.cpp": "llama C P P", "vLLM": "V L L M",
    "GitHub": "git hub", "Git": "git",
    "WireGuard": "wire guard", "OpenVPN": "open V P N", "Mullvad": "/mˈʊlvɑːd/", "Tailscale": "tail scale",
    "Headscale": "head scale",
    "tmux": "T mux", "xterm.js": "X term J S", "QtWebEngine": "cute web engine",
    "AppImage": "app image", "App": "app",
    "doctor": "doctor", "build": "build",
    "Browser": "browser", "Canary": "canary", "Desktop": "desktop", "Download": "download",
    "Downloads": "downloads", "Exit": "exit", "Fetch": "fetch", "Key": "key", "Offline": "offline",
    "Release": "release", "Servers": "servers", "Tool": "tool", "live": "live", "macOS": "mac O S", "npm": "N P M",
    "Drag and Drop": "drag and drop", "Debian": "/dˈɛbiən/", "Python": "/pˈaɪθən/", "Proxmox": "/pɹˈɒksmɒks/",
    "APIs": "/ˌeɪpiːˈaɪz/", "HOMEs": "homes", "JSON": "/dʒˈeɪsən/", "Repository": "repository",
    "URLs": "/jˌuːɑːɹˈɛlz/", "extend": "extend", "images": "images",
    "CPUs": "/sˌiːpˌiːjˈuːz/", "Loop Guard": "loop guard", "Pod": "pod", "Registry": "registry",
    "Sampling": "sampling", "Service": "service", "Services": "services", "Tools": "tools",
    "Bytes": "bytes", "Code": "code", "Memory": "memory", "HTTP": "H T T P", "Commit": "commit",
    # the file manager's remote pane, keys, the credentials given to a running sandbox
    "SFTP": "S F T P", "OpenSSH": "open S S H", "Esc": "escape", "Alt": "alt",
    "git push": "git push", "secrets give": "secrets give", "git-logins give": "git logins give", "revoke": "revoke",
}

# Words of the German narration that English texts share, said the German way on purpose (German words, names
# said alike in both languages, IT words that are German words too).
GERMAN = {
    # German words that happen to be English words too
    "in", "an", "am", "so", "was", "also", "man", "hat", "will", "die", "den", "fast", "rot", "Art", "alle",
    "alles", "Name", "Namen", "Start", "Starts", "Ende", "Agent", "Agenten", "Status", "Filter", "Terminal",
    "Terminals", "Editor", "Hex-Editor", "Text", "Teil", "Teile", "Modell", "Modelle", "Port", "Netz", "System",
    "Team", "Teams", "Optionen", "Option", "Profil", "Profile", "Dateien", "Datei", "Ordner", "Hilfe", "Galerie",
    "Statistik", "Aktivität", "Verbindungen", "Freigaben", "Zugangsdaten", "Bericht", "Konzepte", "Funktion",
    "Details", "Graph", "Kategorien", "Variante", "Varianten", "Validator", "Kopie", "Kopien", "Link", "Links",
    "Symbolische", "Ziel", "Ziele", "Daten", "Prozess", "Version", "Adresse", "Zeit", "Info", "Telemetrie",
    "Internet", "Firewall", "Abschnitte", "Touren", "Tour", "Strg", "Umschalt", "Enter", "Steuerung",
    "plus", "zu", "Leitung", "Mitglied", "Mitglieder", "Brett", "Liste", "Signatur", "Spalte", "Zeile", "Zeilen",
    "Methode", "Pfad", "Pfade", "Befehl", "Befehle", "Definition", "Detail", "Fragment", "Moment",
    "Plan", "Argument", "private", "Rest", "Standard", "Was", "In", "per", "Tabelle", "Hex", "Codenamen",
    "Platzhalter", "Infobereich", "Schleier", "Dialog", "Dialoge", "Tunnel", "Mitte", "Minute", "Minuten",
    "Sekunden", "Galerie", "Abzeichen", "Integration", "Kategorie", "Konto", "Kontos", "Bash", "Paket", "Pakete",
    "Fenster", "Knopf", "Knöpfe", "Reiter", "Reitern", "Kontextmenü", "Funktionstasten", "Doppelklick",
    "Rechtsklick", "Unsigniert", "signiert", "Katalog", "Kataloge", "Katalogen", "Websuche", "Binäranalyse",
    "Schritt", "Expert", "An", "null",
    # file and key names said as they are written, letter by letter where the voice spells them
    "F", "H", "V", "So", "Tabellen", "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "L", "X", "b", "d", "C", "A",
    # names
    "Thorsten", "Anthropic",
}

# Symbols said as words (narrate.spoken), in this order.
SPOKEN_SYMBOLS = (
    ("&", " und "), ("…", ""), ("›", ","), ("→", ","), (" – ", ", "), (" - ", ", "), ("„", ""), ("“", ""),
    ('"', ""), ("Strg+", "Steuerung plus "), ("Umschalt+", "Umschalt plus "), ("Strg ", "Steuerung "),
)
