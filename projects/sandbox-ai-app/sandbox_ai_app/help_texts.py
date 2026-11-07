"""The texts of the (?) buttons: for every page, area and component of the app, what it is, what it is for, what
happens when you use it, the command-line option behind it and the section of the Help area that says more.
No Qt (help_view.py shows them).

A (?) is identified by a stable id ("page.board", "kind.mount", "board.palette", "settings.window" ...). The texts
are kept here in both languages (say() picks the app's language or the one asked for). The page texts are the
guided tour's (guide.py) and the board's part texts are the parts' own help (board.KINDS), with their German from
i18n.py, so a text is written once. PLACES says where the window puts a (?) on each page: next to a control
("after"), in the corner of an area ("corner") or in the title row of the group around it ("group").

Tests check that every id has both languages and a Help section that exists, and that every page has (?)s.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import i18n
from .tours import Text


@dataclass(frozen=True)
class Tip:
    id: str
    title: Text
    what: Text            # what it is and what it is for
    does: Text | None     # what happens when you use it
    cli: str = ""         # the command-line option or command behind it (the same in both languages)
    section: str = ""     # the Help area's section with more (help_content.BY_ID)


def say(text, language=None):
    """TEXT in LANGUAGE (default: the app's)."""
    return text.de if (language or i18n.language()) == "de" else text.en


def both(key):
    """A text of i18n.py (an N_ key) in both languages."""
    entry = i18n.TEXTS.get(key) or {}
    return Text(key, entry.get("de") or key)


def _tip(ident, title, what, does=None, cli="", section=""):
    return Tip(ident, Text(*title), Text(*what), Text(*does) if does else None, cli, section)


# -- pages: the guided tour's texts (guide.TOUR) --

PAGE_SECTIONS = {"launch": "page-launch", "board": "page-board", "sandboxes": "page-sandboxes",
                 "approvals": "page-approvals", "terminals": "page-terminals", "connections": "page-connections",
                 "activity": "page-activity", "statistics": "page-statistics", "files": "page-files",
                 "kits": "page-kits", "mcp": "page-mcp", "images": "page-images", "credentials": "page-credentials",
                 "system": "page-system", "hosts": "page-hosts", "help": "page-help"}
PAGE_CLI = {"launch": "sandbox-ai AGENT [options]", "board": ".sandbox-ai.json",
            "sandboxes": "sandbox-ai list",
            "approvals": "--ask", "terminals": "tmux", "connections": "sandbox-ai logs, --log-dir",
            "activity": "sandbox-ai activity",
            "statistics": "sandbox-ai activity", "files": "", "kits": "sandbox-ai kits, --kit",
            "mcp": "sandbox-ai mcp, --mcp", "images": "sandbox-ai build", "credentials": "sandbox-ai secrets",
            "system": "sandbox-ai doctor", "hosts": "sandbox-ai hosts", "help": ""}


def _page_tips():
    from . import guide
    found = {}
    for step in guide.TOUR:
        found[f"page.{step.page}"] = Tip(f"page.{step.page}", both(step.title), both(step.text), both(step.try_),
                                         PAGE_CLI.get(step.page, ""), PAGE_SECTIONS[step.page])
    return found


# -- the parts of the board: their own help (board.KINDS) and what adding one does --

KIND_SECTIONS = {"workspace": "workspaces-mounts", "mount": "workspaces-mounts", "home": "secrets", "kits": "kits",
                 "agent": "sandbox", "approvals": "approvals", "proxy": "proxy", "git": "secrets", "mcp": "mcp",
                 "llm": "llm", "network": "network", "vpn": "vpn-tor", "devices": "devices", "limits": "limits",
                 "display": "display", "session": "sessions", "logs": "logs", "engine": "sandbox"}
KIND_DOES = {
    "workspace": ("Connected to the agent, its folder is mounted: the first at /work (the project file is written "
                  "there), further ones at /workspaces/NAME. Not connected, it does not count.",
                  "Mit dem Agenten verbunden, wird sein Ordner eingebunden: der erste unter /work (dort wird die "
                  "Projektdatei geschrieben), weitere unter /workspaces/NAME. Nicht verbunden, zählt er nicht."),
    "mount": ("Connected to the agent, the host folder or file appears at the path you give, read-only unless "
              "you choose writable.",
              "Mit dem Agenten verbunden, erscheint der Ordner oder die Datei des Hosts unter dem angegebenen Pfad, "
              "nur lesbar, außer Sie wählen beschreibbar."),
    "home": ("Decides whether the sandbox HOME is kept between runs and how the agent's login gets in.",
             "Bestimmt, ob das HOME der Sandbox zwischen Läufen bleibt und wie der Login des Agenten hineinkommt."),
    "kits": ("The kits you tick are copied into HOME at the start; memory and plugins can be switched off.",
             "Die angehakten Kits werden beim Start ins HOME kopiert; Memory und Plugins lassen sich abschalten."),
    "agent": ("Always on the board: which agent runs, its model and arguments. Everything else connects to it.",
              "Immer auf der Tafel: welcher Agent läuft, sein Modell und seine Argumente. Alles andere verbindet sich"
              " mit ihm."),
    "approvals": ("Sets when a running sandbox asks you (Approvals page) and which rulesets allow, ask or deny.",
                  "Legt fest, wann eine laufende Sandbox Sie fragt (Seite Freigaben) und welche Regelwerke "
                  "erlauben, fragen oder ablehnen."),
    "proxy": ("Always on the board: every connection passes it. Off (--no-proxy) only where the policy allows; "
              "request rules per host narrow what a host may receive.",
              "Immer auf der Tafel: Jede Verbindung läuft hindurch. Aus (--no-proxy) nur, wo die Richtlinie es "
              "erlaubt; Anfrageregeln pro Host begrenzen, was ein Host bekommen darf."),
    "git": ("Commit identity and push tokens; with the token proxy the agent never sees a token.",
            "Commit-Identität und Push-Tokens; mit dem Token-Proxy sieht der Agent nie ein Token."),
    "mcp": ("Connected to the agent, the server is started in its own sandbox and the agent reaches it through a "
            "relay that applies your rules.",
            "Mit dem Agenten verbunden, startet der Server in einer eigenen Sandbox, und der Agent erreicht ihn "
            "über ein Relay, das Ihre Regeln anwendet."),
    "llm": ("Opens only the address and port of a server registered with sandbox-ai llm; its key stays with the "
            "proxy.",
            "Öffnet nur Adresse und Port eines mit sandbox-ai llm eingetragenen Servers; sein Schlüssel bleibt "
            "beim Proxy."),
    "network": ("Always on the board: profiles, allowed hosts, addresses, ports and DNS. Nothing listed here is "
                "reachable (except the agent's provider).",
                "Immer auf der Tafel: Profile, erlaubte Hosts, Adressen, Ports und DNS. Was hier nicht steht, ist "
                "nicht erreichbar (außer dem Anbieter des Agenten)."),
    "vpn": ("Every connection leaves through the profile's tunnel; a kill switch blocks the rest, and the start "
            "checks addresses and DNS first.",
            "Jede Verbindung geht durch den Tunnel des Profils; ein Kill-Switch sperrt den Rest, und der Start "
            "prüft vorher Adressen und DNS."),
    "devices": ("The devices you list are handed to the agent. Each one weakens the isolation: list only what "
                "the task needs.",
                "Die aufgeführten Geräte werden dem Agenten gegeben. Jedes schwächt die Isolation: Nennen Sie nur, "
                "was die Aufgabe braucht."),
    "limits": ("Caps memory, CPUs and processes; a running sandbox of the project can get new limits at once.",
               "Begrenzt Speicher, CPUs und Prozesse; eine laufende Sandbox des Projekts kann sofort neue Grenzen "
               "bekommen."),
    "display": ("Starts a virtual display (or VNC, or your Wayland) so GUI programs and browsers can run.",
                "Startet eine virtuelle Anzeige (oder VNC, oder Ihr Wayland), damit GUI-Programme und Browser "
                "laufen können."),
    "session": ("tmux inside the sandbox or not; a session itself is started with Start as session.",
                "tmux in der Sandbox oder nicht; eine Sitzung selbst starten Sie mit Als Sitzung starten."),
    "logs": ("Where the connection and file logs go (outside the sandbox) and how much they keep.",
             "Wohin die Verbindungs- und Dateilogs gehen (außerhalb der Sandbox) und wie viel sie behalten."),
    "engine": ("Podman or Bubblewrap, the image or variant, and the agent's executable.",
               "Podman oder Bubblewrap, das Image oder die Variante und die ausführbare Datei des Agenten."),
}


def _kind_tips():
    from . import board as B
    found = {}
    for key, kind in B.KINDS.items():
        options = ", ".join(kind.options[:6]) + (", …" if len(kind.options) > 6 else "")
        cli = {"workspace": "--workspace, --workspace-ro", "mount": "--mount SRC:DEST[:ro|rw]", "mcp": "--mcp NAME",
               "proxy": "--no-proxy, --allow-http, --no-inspect"}.get(key, options)
        found[f"kind.{key}"] = Tip(f"kind.{key}", both(kind.title), both(kind.help), Text(*KIND_DOES[key]), cli,
                                   KIND_SECTIONS[key])
    return found


# -- areas and controls --

AREA_TIPS = (
    # Start
    _tip("launch.agent", ("Agent", "Agent"),
         ("The coding agent the sandbox runs: Claude Code, Codex or OpenCode, each in its own image.",
          "Der Coding-Agent, den die Sandbox ausführt: Claude Code, Codex oder OpenCode, jeder in seinem eigenen "
          "Image."),
         ("The form below shows the options of this agent; the command line follows.",
          "Das Formular darunter zeigt die Optionen dieses Agenten; die Befehlszeile folgt."),
         "sandbox-ai claude | codex | opencode", "page-launch"),
    _tip("launch.profile", ("Profile", "Profil"),
         ("Your saved option sets for the Start page (Save as profile…), loaded with Load.",
          "Ihre gespeicherten Optionssätze der Seite Start (Als Profil speichern…), geladen mit Laden."),
         ("Load fills the whole form with the profile; nothing starts until you press a start button.",
          "Laden füllt das ganze Formular mit dem Profil; nichts startet, bevor Sie einen Start-Knopf drücken."),
         "", "page-launch"),
    _tip("launch.form", ("Every option", "Alle Optionen"),
         ("All launcher options of the agent in groups: network, proxy and privacy, Git, logins, kits, display, "
          "hardware. What the administrator's policy forbids is greyed out.",
          "Alle Optionen des Launchers in Gruppen: Netzwerk, Proxy und Datenschutz, Git, Logins, Kits, Anzeige, "
          "Hardware. Was die Richtlinie des Administrators verbietet, ist ausgegraut."),
         ("Each change updates the command line below at once; fields that widen the sandbox are marked.",
          "Jede Änderung aktualisiert sofort die Befehlszeile darunter; Felder, die die Sandbox erweitern, sind "
          "markiert."),
         "sandbox-ai AGENT --help", "page-launch"),
    _tip("launch.preview", ("The command line", "Die Befehlszeile"),
         ("Exactly what the app runs for a start, as you would type it in a terminal.",
          "Genau das, was die App für einen Start ausführt, so wie Sie es in einem Terminal tippen würden."),
         ("Copy it to start the same sandbox from a shell, a script or another machine.",
          "Kopieren Sie sie, um dieselbe Sandbox aus einer Shell, einem Skript oder auf einem anderen Rechner zu "
          "starten."),
         "", "page-launch"),
    _tip("launch.dry", ("Dry run", "Probelauf"),
         ("The launcher checks options, policy, images and network and shows its plan.",
          "Der Launcher prüft Optionen, Richtlinie, Images und Netzwerk und zeigt seinen Plan."),
         ("Nothing is started and nothing is changed; a refusal says which option is wrong.",
          "Nichts wird gestartet und nichts geändert; eine Ablehnung nennt die falsche Option."),
         "--dry-run", "page-launch"),
    _tip("launch.session", ("Session", "Sitzung"),
         ("A named sandbox that keeps running when you close the terminal or the app.",
          "Eine benannte Sandbox, die weiterläuft, wenn Sie das Terminal oder die App schließen."),
         ("Start as session starts it detached; attach, pause, resume and stop it on Sandboxes & sessions.",
          "Als Sitzung starten startet sie losgelöst; Verbinden, Pausieren, Fortsetzen und Beenden auf Sandboxen & "
          "Sitzungen."),
         "--session NAME", "sessions"),
    # Board
    _tip("board.palette", ("Parts", "Teile"),
         ("Everything you can add to the sandbox: folders, network, MCP and LLM servers, VPN, devices, limits and "
          "more. Parts that may exist once and are on the board already are greyed out.",
          "Alles, was Sie der Sandbox hinzufügen können: Ordner, Netzwerk, MCP- und LLM-Server, VPN, Geräte, Grenzen "
          "und mehr. Teile, die es nur einmal geben darf und die schon auf der Tafel sind, sind ausgegraut."),
         ("Drag a part onto the board to place it where you drop it, or double-click it (or press Enter) to add it. "
          "The guide then asks what it needs. The (?) of a row explains that part.",
          "Ziehen Sie ein Teil auf die Tafel, um es dort abzulegen, oder doppelklicken Sie es (oder drücken Sie "
          "Enter), um es hinzuzufügen. Der Guide fragt dann, was es braucht. Das (?) einer Zeile erklärt dieses Teil."),
         "", "page-board"),
    _tip("board.canvas", ("The board", "Die Tafel"),
         ("One sandbox as parts and connections. An arrow means flow: folders give the agent its files, its "
          "connections pass the proxy to the network.",
          "Eine Sandbox als Teile und Verbindungen. Ein Pfeil bedeutet Fluss: Ordner geben dem Agenten seine "
          "Dateien, seine Verbindungen laufen über den Proxy ins Netzwerk."),
         ("Drag a node to move it; drag from a node's right port onto another node to connect them; Delete removes "
          "the selection. Drop folders here to add them as workspace or mount, a .sandbox-ai.json to open its "
          "project, a preset file to import it. Every change is one step of Undo.",
          "Ziehen Sie einen Knoten, um ihn zu verschieben; ziehen Sie vom rechten Anschluss eines Knotens auf einen "
          "anderen, um sie zu verbinden; Entf entfernt die Auswahl. Legen Sie hier Ordner ab, um sie als "
          "Arbeitsordner oder Einbindung hinzuzufügen, eine .sandbox-ai.json, um ihr Projekt zu öffnen, eine "
          "Vorlagen-Datei, um sie zu importieren. Jede Änderung ist ein Schritt von Rückgängig."),
         ".sandbox-ai.json", "page-board"),
    _tip("board.inspector", ("Inspector", "Inspektor"),
         ("The settings of the selected part, built from the launcher's own options with their help texts.",
          "Die Einstellungen des gewählten Teils, aufgebaut aus den Optionen des Launchers mit ihren Hilfetexten."),
         ("A change applies to the board at once and is one step of Undo; Set up step by step… asks the "
          "questions again.",
          "Eine Änderung gilt sofort auf der Tafel und ist ein Schritt von Rückgängig; Schritt für Schritt "
          "einrichten… stellt die Fragen erneut."),
         "", "page-board"),
    _tip("board.findings", ("Findings and score", "Befunde und Punktzahl"),
         ("What the launcher or the policy would refuse (✘), what makes little sense (!), and what weakens the "
          "sandbox, with a security score of 100.",
          "Was der Launcher oder die Richtlinie ablehnen würde (✘), was wenig Sinn ergibt (!) und was die Sandbox "
          "schwächt, mit einer Sicherheitspunktzahl von 100."),
         ("Double-click a finding to select its part. Apply and Start refuse while there is an error.",
          "Doppelklicken Sie einen Befund, um sein Teil zu wählen. Übernehmen und Starten verweigern, solange es "
          "einen Fehler gibt."),
         "", "page-board"),
    _tip("board.command", ("Command line and verdict", "Befehlszeile und Urteil"),
         ("The board as the launcher's command line, and below it the launcher's own verdict from a dry run in "
          "the background after each change.",
          "Die Tafel als Befehlszeile des Launchers und darunter das Urteil des Launchers aus einem Probelauf im "
          "Hintergrund nach jeder Änderung."),
         None, "sandbox-ai AGENT --dry-run", "page-board"),
    _tip("board.apply", ("Apply", "Übernehmen"),
         ("Writes the board as .sandbox-ai.json into the first workspace and trusts exactly that content; the "
          "layout goes into .sandbox-ai.board.json.",
          "Schreibt die Tafel als .sandbox-ai.json in den ersten Arbeitsordner und vertraut genau diesem Inhalt; das "
          "Layout geht in .sandbox-ai.board.json."),
         ("You see the file and what changes before anything is written; later starts of the project use it.",
          "Sie sehen die Datei und was sich ändert, bevor etwas geschrieben wird; spätere Starts des Projekts "
          "nutzen sie."),
         "sandbox-ai wizard trust", "page-board"),
    _tip("board.dry", ("Dry run", "Probelauf"),
         ("Asks the launcher about exactly this board and shows its plan.",
          "Fragt den Launcher nach genau dieser Tafel und zeigt seinen Plan."),
         ("Nothing starts; a refusal names the option it does not accept.",
          "Nichts startet; eine Ablehnung nennt die Option, die er nicht annimmt."),
         "--dry-run", "page-board"),
    _tip("board.presets", ("Presets and wizard", "Vorlagen und Assistent"),
         ("New from preset… opens the gallery: ready boards, sandbox-ai's presets and your own. Wizard… builds a "
          "new project step by step.",
          "Neu aus Vorlage… öffnet die Galerie: fertige Tafeln, die Presets von sandbox-ai und Ihre eigenen Vorlagen."
          " Assistent… baut ein neues Projekt Schritt für Schritt."),
         ("In the gallery, Save the board as a preset… keeps the board shown now as your own preset.",
          "In der Galerie speichert Tafel als Vorlage speichern… die gezeigte Tafel als Ihre eigene Vorlage."),
         "sandbox-ai presets list", "app-presets-gallery"),
    _tip("board.tools", ("Undo, arrange, zoom", "Rückgängig, Anordnen, Zoom"),
         ("Undo and Redo go through every change of this project's board; Arrange lays the parts out in columns; "
          "+, − and Fit zoom.",
          "Rückgängig und Wiederholen gehen durch jede Änderung an der Tafel dieses Projekts; Anordnen legt die Teile"
          " in Spalten; +, − und Einpassen zoomen."),
         ("Ctrl and the mouse wheel zoom too; the middle mouse button pans.",
          "Strg und das Mausrad zoomen ebenfalls; die mittlere Maustaste verschiebt."),
         "", "page-board"),
    # Sandboxes & sessions
    _tip("sandboxes.table", ("Sandboxes, sessions, MCP servers", "Sandboxen, Sitzungen, MCP-Server"),
         ("Everything that runs or is kept on this computer, with kind, project, agent, backend and start time; "
          "rows of the current project are bold.",
          "Alles, was auf diesem Rechner läuft oder aufbewahrt wird, mit Art, Projekt, Agent, Backend und "
          "Startzeit; Zeilen des aktuellen Projekts sind fett."),
         ("Select a row for its details and the buttons that act on it.",
          "Wählen Sie eine Zeile für ihre Details und die Knöpfe, die auf sie wirken."),
         "sandbox-ai list", "sessions"),
    _tip("sandboxes.only_project", ("Only the current project", "Nur das aktuelle Projekt"),
         ("Limits the list to the sandboxes and sessions of the project open now.",
          "Beschränkt die Liste auf die Sandboxen und Sitzungen des gerade offenen Projekts."),
         None, "", "page-sandboxes"),
    _tip("sandboxes.stop", ("Stop", "Beenden"),
         ("Ends the selected sandbox after asking; its launcher cleans up. A session can be paused instead.",
          "Beendet die gewählte Sandbox nach Rückfrage; ihr Launcher räumt auf. Eine Sitzung lässt sich stattdessen "
          "pausieren."),
         None, "sandbox-ai stop NAME", "sessions"),
    _tip("sandboxes.live", ("While it runs", "Während sie läuft"),
         ("Changes for a running sandbox: allow or revoke a host, hand over a device, change the limits, show "
          "its log.",
          "Änderungen für eine laufende Sandbox: einen Host erlauben oder entziehen, ein Gerät geben, die Grenzen "
          "ändern, ihr Log zeigen."),
         ("They last until the sandbox ends; the project file stays as it is.",
          "Sie gelten, bis die Sandbox endet; die Projektdatei bleibt, wie sie ist."),
         "sandbox-ai net allow | sandbox-ai devices | sandbox-ai limits", "network"),
    # Approvals
    _tip("approvals.open", ("Open questions", "Offene Fragen"),
         ("What running sandboxes ask: a destination not allowed yet, a push with warnings, an MCP tool call, a "
          "page to open.",
          "Was laufende Sandboxen fragen: ein noch nicht erlaubtes Ziel, ein Push mit Warnungen, ein MCP-"
          "Werkzeugaufruf, eine zu öffnende Seite."),
         ("The sandbox waits for your answer; without one in time the request is refused.",
          "Die Sandbox wartet auf Ihre Antwort; ohne Antwort rechtzeitig wird die Anfrage abgelehnt."),
         "--ask", "approvals"),
    _tip("approvals.scope", ("For how long", "Für wie lange"),
         ("Once, for this run, or always (kept for the project). The security mode decides what is offered.",
          "Einmal, für diesen Lauf oder immer (für das Projekt gemerkt). Der Sicherheitsmodus bestimmt, was "
          "angeboten wird."),
         ("Allow, Deny and Block use the scope chosen here.",
          "Erlauben, Ablehnen und Sperren nutzen den hier gewählten Umfang."),
         "--mode", "approvals"),
    _tip("approvals.always", ("Allowed always", "Immer erlaubt"),
         ("Your standing answers per project.", "Ihre dauerhaften Antworten pro Projekt."),
         ("Forget one and the sandbox asks again the next time.",
          "Vergessen Sie eine, fragt die Sandbox beim nächsten Mal wieder."),
         "", "approvals"),
    # Terminals
    _tip("terminals.tabs", ("Terminal tabs", "Terminal-Tabs"),
         ("The sandboxes started in the app, builds and logins, each in a tab: the agent in tmux window 0, a shell "
          "in window 1.",
          "Die in der App gestarteten Sandboxen, Builds und Logins, jede in einem Tab: der Agent in tmux-Fenster 0, "
          "eine Shell in Fenster 1."),
         ("Ctrl-b d detaches from a session, which keeps running; closing a tab of a one-off run ends it.",
          "Strg-b d löst sich von einer Sitzung, die weiterläuft; das Schließen des Tabs eines einmaligen Laufs "
          "beendet ihn."),
         "--tmux", "page-terminals"),
    # Connections
    _tip("connections.directory", ("Log folder", "Log-Ordner"),
         ("Which logs are read: the project's log folder, one of an earlier run, or one you choose.",
          "Welche Logs gelesen werden: der Log-Ordner des Projekts, einer eines früheren Laufs oder einer Ihrer "
          "Wahl."),
         None, "sandbox-ai logs, --log-dir", "logs"),
    _tip("connections.filter", ("Filter", "Filter"),
         ("Shows only requests whose host, path or method contains the text; only refused shows what was not let "
          "through.",
          "Zeigt nur Anfragen, deren Host, Pfad oder Methode den Text enthält; nur Abgelehntes und Fehler zeigt, was "
          "nicht durchgelassen wurde."),
         None, "", "page-connections"),
    _tip("connections.table", ("Connections", "Verbindungen"),
         ("Every connection the proxy saw: allowed, asked or refused, with method, status, target and size.",
          "Jede Verbindung, die der Proxy sah: erlaubt, gefragt oder abgewiesen, mit Methode, Status, Ziel und "
          "Größe."),
         ("Select a row to see the request in detail below.", "Wählen Sie eine Zeile, um die Anfrage unten im "
          "Detail zu sehen."),
         "", "proxy"),
    _tip("connections.detail", ("One request", "Eine Anfrage"),
         ("Method, URL, headers and bodies (credentials redacted), the rule that decided and the option that "
          "would allow a refused one.",
          "Methode, URL, Header und Bodies (Zugangsdaten geschwärzt), die Regel, die entschied, und die Option, die "
          "eine abgelehnte erlauben würde."),
         None, "--allow-host, --allow-http", "network"),
    # Activity
    _tip("activity.source", ("Which run", "Welcher Lauf"),
         ("The run whose activity is shown: prompts, replies, tool calls, commands, files, connections, questions "
          "and the taint, in order.",
          "Der Lauf, dessen Aktivität gezeigt wird: Prompts, Antworten, Werkzeugaufrufe, Befehle, Dateien, "
          "Verbindungen, Fragen und der Taint, der Reihe nach."),
         None, "sandbox-ai activity", "logs"),
    _tip("activity.kinds", ("Kinds", "Arten"),
         ("Chooses which kinds of events are listed.", "Wählt, welche Arten von Ereignissen gezeigt werden."),
         None, "", "page-activity"),
    _tip("activity.table", ("Events", "Ereignisse"),
         ("The run's events in order; with follow on, new ones appear as they happen.",
          "Die Ereignisse des Laufs der Reihe nach; mit mitlaufen erscheinen neue, sobald sie geschehen."),
         ("Select one to see everything about it below.", "Wählen Sie eines, um unten alles darüber zu sehen."),
         "", "page-activity"),
    _tip("activity.detail", ("One event", "Ein Ereignis"),
         ("Everything recorded about the selected event; from here to its connection or file.",
          "Alles, was zum gewählten Ereignis aufgezeichnet ist; von hier zu seiner Verbindung oder Datei."),
         None, "", "page-activity"),
    # Statistics
    _tip("statistics.directory", ("Which project", "Welches Projekt"),
         ("The log folder whose runs are counted: tokens, requests and tool calls per run and per agent.",
          "Der Log-Ordner, dessen Läufe gezählt werden: Tokens, Anfragen und Werkzeugaufrufe pro Lauf und pro "
          "Agent."),
         None, "--log-dir", "logs"),
    # Files
    _tip("files.roots", ("Whose files", "Wessen Dateien"),
         ("The HOME and workspaces of a sandbox, or a project folder.",
          "HOME und Arbeitsordner einer Sandbox oder ein Projektordner."),
         ("Choose one to browse it without opening a shell in the sandbox.",
          "Wählen Sie eines, um es zu durchsuchen, ohne eine Shell in der Sandbox zu öffnen."),
         "", "page-files"),
    _tip("files.table", ("Files", "Dateien"),
         ("The files of the chosen folder. Links are shown with where they point but never followed.",
          "Die Dateien des gewählten Ordners. Links werden mit ihrem Ziel gezeigt, aber nie verfolgt."),
         ("Copy a file in or out with the buttons below: no mount is needed.",
          "Kopieren Sie eine Datei mit den Knöpfen darunter hinein oder heraus: Es braucht keine Einbindung."),
         "", "page-files"),
    _tip("files.replace", ("Replace existing files", "Vorhandene Dateien ersetzen"),
         ("Allows a copy to overwrite a file of the same name; off, nothing is ever replaced.",
          "Erlaubt einer Kopie, eine gleichnamige Datei zu überschreiben; aus, wird nie etwas ersetzt."),
         None, "", "page-files"),
    # Kits & plugins
    _tip("kits.table", ("Kits", "Kits"),
         ("Skills, agents, commands, prompts, memory templates, hooks and plugins, bundled in kits: yours and "
          "sandbox-ai's built-in ones.",
          "Skills, Agenten, Befehle, Prompts, Memory-Vorlagen, Hooks und Plugins, gebündelt in Kits: Ihre und die "
          "eingebauten von sandbox-ai."),
         ("Select one to see what it brings; a project uses it with --kit.",
          "Wählen Sie eines, um zu sehen, was es mitbringt; ein Projekt nutzt es mit --kit."),
         "sandbox-ai kits list", "kits"),
    _tip("kits.use", ("Use in the current project", "Im aktuellen Projekt nutzen"),
         ("Adds the kit to the kits part of the project's board and applies it.",
          "Fügt das Kit dem Kits-Teil der Tafel des Projekts hinzu und übernimmt es."),
         ("You see the new .sandbox-ai.json before it is written and trusted.",
          "Sie sehen die neue .sandbox-ai.json, bevor sie geschrieben und ihr vertraut wird."),
         "--kit NAME", "kits"),
    _tip("kits.hooks", ("Approve hooks", "Hooks freigeben"),
         ("A hook runs code at the agent's events. None runs before you approved its exact text.",
          "Ein Hook führt Code bei Ereignissen des Agenten aus. Keiner läuft, bevor Sie seinen genauen Text "
          "freigegeben haben."),
         ("A changed hook needs approval again.", "Ein geänderter Hook braucht erneut eine Freigabe."),
         "sandbox-ai kits trust-hooks", "kits"),
    _tip("kits.skill", ("New skill", "Neuer Skill"),
         ("A SKILL.md in a kit of yours, which the agent reads when its description fits the task.",
          "Eine SKILL.md in einem Ihrer Kits, die der Agent liest, wenn ihre Beschreibung zur Aufgabe passt."),
         None, "sandbox-ai kits new-skill", "kits"),
    # MCP servers
    _tip("mcp.servers", ("Your MCP servers", "Ihre MCP-Server"),
         ("The servers you defined, each in a sandbox or container of its own with only the network and folders "
          "its definition names.",
          "Die Server, die Sie definiert haben, jeder in einer eigenen Sandbox oder einem eigenen Container, nur "
          "mit dem Netzwerk und den Ordnern, die seine Definition nennt."),
         ("A start names a server with --mcp, or connect an MCP part to the agent on the board.",
          "Ein Start nennt einen Server mit --mcp, oder verbinden Sie ein MCP-Teil auf der Tafel mit dem Agenten."),
         "sandbox-ai mcp list, --mcp NAME", "mcp"),
    _tip("mcp.catalog", ("Catalogs", "Kataloge"),
         ("Audited servers, pinned by digest, ready to add.", "Geprüfte Server, per Digest festgelegt, bereit zum "
          "Hinzufügen."),
         ("Adding one defines it under a name; nothing runs until a start names it.",
          "Hinzufügen definiert ihn unter einem Namen; nichts läuft, bis ein Start ihn nennt."),
         "sandbox-ai mcp catalog | add", "mcp"),
    # Images
    _tip("images.images", ("Images and variants", "Images und Varianten"),
         ("Each agent's image and your variants: built or not, when, how big, and whether the extension changed "
          "since.",
          "Das Image jedes Agenten und Ihre Varianten: gebaut oder nicht, wann, wie groß und ob sich die "
          "Erweiterung seitdem geändert hat."),
         None, "sandbox-ai images", "page-images"),
    _tip("images.build", ("Build", "Bauen"),
         ("Builds the base image, then the extension layer, in a terminal.",
          "Baut das Basis-Image, dann die Erweiterungsschicht, in einem Terminal."),
         ("Needed once per agent and again after an update or a change of the extension.",
          "Nötig einmal pro Agent und wieder nach einem Update oder einer Änderung der Erweiterung."),
         "sandbox-ai build AGENT", "page-images"),
    _tip("images.variant", ("New variant", "Neue Variante"),
         ("A copy of an agent's image with an extension of its own (more packages, a media recipe).",
          "Eine Kopie des Images eines Agenten mit eigener Erweiterung (mehr Pakete, ein Media-Rezept)."),
         None, "--image-variant NAME", "page-images"),
    _tip("images.editor", ("Extension", "Erweiterung"),
         ("Debian packages, npm packages, Python tools, files and a Containerfile fragment, checked as you type; "
          "the final Containerfile is shown too.",
          "Debian-Pakete, npm-Pakete, Python-Werkzeuge, Dateien und ein Containerfile-Fragment, beim Tippen "
          "geprüft; das fertige Containerfile wird auch gezeigt."),
         ("Saving changes the extension; the image changes only when you build again.",
          "Speichern ändert die Erweiterung; das Image ändert sich erst, wenn Sie neu bauen."),
         "", "page-images"),
    # Credentials
    _tip("credentials.logins", ("Agent logins", "Logins der Agenten"),
         ("Each agent's login, kept on this computer outside every sandbox.",
          "Der Login jedes Agenten, auf diesem Rechner außerhalb jeder Sandbox aufbewahrt."),
         ("A sandbox gets a copy when it starts; log in once here.",
          "Eine Sandbox bekommt beim Start eine Kopie; melden Sie sich hier einmal an."),
         "sandbox-ai login AGENT", "secrets"),
    _tip("credentials.secrets", ("Secrets", "Geheimnisse"),
         ("API keys and tokens by name, in your keyring or in private files.",
          "API-Schlüssel und Tokens unter einem Namen, in Ihrem Schlüsselbund oder in privaten Dateien."),
         ("A start only names them (--secret-env NAME); with the token proxy they never enter the sandbox.",
          "Ein Start nennt sie nur (--secret-env NAME); mit dem Token-Proxy kommen sie nie in die Sandbox."),
         "sandbox-ai secrets", "secrets"),
    _tip("credentials.vpn", ("VPN profiles", "VPN-Profile"),
         ("WireGuard, OpenVPN, Mullvad, Tailscale or Tor in front of a sandbox; keys stay with sandbox-ai secrets.",
          "WireGuard, OpenVPN, Mullvad, Tailscale oder Tor vor einer Sandbox; Schlüssel bleiben bei sandbox-ai "
          "secrets."),
         ("A start uses one with --vpn NAME, or connect a VPN part on the board.",
          "Ein Start nutzt eines mit --vpn NAME, oder verbinden Sie ein VPN-Teil auf der Tafel."),
         "sandbox-ai vpn add | list, --vpn NAME", "vpn-tor"),
    # System
    _tip("system.checks", ("Host check", "Host-Prüfung"),
         ("What sandbox-ai needs on this computer, checked by sandbox-ai doctor: Podman or Bubblewrap, images, "
          "network, keyring.",
          "Was sandbox-ai auf diesem Rechner braucht, geprüft von sandbox-ai doctor: Podman oder Bubblewrap, "
          "Images, Netzwerk, Schlüsselbund."),
         ("Each failed check comes with what fixes it.", "Jede fehlgeschlagene Prüfung kommt mit dem, was sie "
          "behebt."),
         "sandbox-ai doctor", "sandbox"),
    _tip("system.settings", ("Settings of the app", "Einstellungen der App"),
         ("Theme, language, the system tray and the terminal; All settings… has the rest.",
          "Thema, Sprache, System-Tray und Terminal; Alle Einstellungen… hat den Rest."),
         ("The theme applies at once, a new language at the next start.",
          "Das Thema gilt sofort, eine neue Sprache beim nächsten Start."),
         "", "app-settings"),
    _tip("system.update", ("Check for updates", "Auf Updates prüfen"),
         ("Looks for a newer sandbox-ai and app.", "Sucht nach einem neueren sandbox-ai und einer neueren App."),
         ("The old version stays until the new one works.", "Die alte Version bleibt, bis die neue funktioniert."),
         "sandbox-ai update", "releases-update"),
    _tip("system.policy", ("Administrator policy", "Richtlinie des Administrators"),
         ("/etc/sandbox-ai/policy.json: what the administrator forbids or requires for every start.",
          "/etc/sandbox-ai/policy.json: was der Administrator für jeden Start verbietet oder verlangt."),
         ("The app greys forbidden options out everywhere.", "Die App graut verbotene Optionen überall aus."),
         "", "policy"),
    # Hosts
    _tip("hosts.list", ("Hosts", "Hosts"),
         ("Other machines whose sandbox-ai this app manages over SSH, such as LXC containers.",
          "Andere Rechner, deren sandbox-ai diese App über SSH verwaltet, etwa LXC-Container."),
         ("Select one to see what runs there; Test runs its sandbox-ai doctor.",
          "Wählen Sie einen, um zu sehen, was dort läuft; Testen führt sein sandbox-ai doctor aus."),
         "sandbox-ai hosts", "hosts"),
    _tip("hosts.tabs", ("On the host", "Auf dem Host"),
         ("The host's starts and sessions and its open questions.",
          "Die Starts und Sitzungen des Hosts und seine offenen Fragen."),
         ("Attach opens a session's terminal here.", "Verbinden öffnet das Terminal einer Sitzung hier."),
         "", "hosts"),
    # Help
    _tip("help.search", ("Search the help", "Hilfe durchsuchen"),
         ("Finds sections whose title or text contains every word you type, in English and German.",
          "Findet Abschnitte, deren Titel oder Text jedes eingegebene Wort enthält, auf Englisch und Deutsch."),
         None, "", "page-help"),
    _tip("help.language", ("Language of the help", "Sprache der Hilfe"),
         ("Shows the help in the app's language, or in English or German.",
          "Zeigt die Hilfe in der Sprache der App oder auf Englisch oder Deutsch."),
         None, "", "page-help"),
    _tip("help.tours", ("Tours", "Touren"),
         ("Guided walks through whole tasks across pages, and a tour of each page.",
          "Geführte Wege durch ganze Aufgaben über mehrere Seiten und eine Tour jeder Seite."),
         ("A tour outlines each control it talks about and ticks a step off when you have done it.",
          "Eine Tour umrandet jedes Bedienelement, von dem sie spricht, und hakt einen Schritt ab, wenn Sie ihn "
          "erledigt haben."),
         "", "app-tours"),
    # Settings dialog: one per tab
    _tip("settings.appearance", ("Appearance", "Aussehen"),
         ("Theme and language, the guide for new board parts, the guided tour and the intro.",
          "Thema und Sprache, der Guide für neue Teile der Tafel, die geführte Tour und das Intro."),
         ("Each field is saved when it changes; a new language applies at the next start.",
          "Jedes Feld wird gespeichert, wenn es sich ändert; eine neue Sprache gilt beim nächsten Start."),
         "", "app-settings"),
    _tip("settings.window", ("Window", "Fenster"),
         ("The system tray, start at login, where commands run, notifications, tabs for new sessions, debug "
          "logging.",
          "System-Tray, Start bei der Anmeldung, wo Befehle laufen, Benachrichtigungen, Tabs für neue Sitzungen, "
          "Debug-Protokoll."),
         None, "", "app-settings"),
    _tip("settings.polling", ("Polling", "Abfragen"),
         ("How often the app looks for new questions, running sandboxes and new connections.",
          "Wie oft die App nach neuen Fragen, laufenden Sandboxen und neuen Verbindungen sieht."),
         ("Keep the question interval short: a question without an answer is refused after a timeout.",
          "Halten Sie das Frage-Intervall kurz: Eine Frage ohne Antwort wird nach einer Frist abgelehnt."),
         "", "approvals"),
    _tip("settings.sandbox-ai", ("sandbox-ai", "sandbox-ai"),
         ("Which sandbox-ai the app uses: the sandbox-ai command, $SANDBOX_AI_HOME or a checkout you choose.",
          "Welches sandbox-ai die App nutzt: den Befehl sandbox-ai, $SANDBOX_AI_HOME oder einen gewählten "
          "Checkout."),
         ("Applies at the next start; a folder a sandbox can write is refused.",
          "Gilt beim nächsten Start; ein Ordner, den eine Sandbox schreiben kann, wird abgelehnt."),
         "SANDBOX_AI_HOME", "releases-update"),
    _tip("settings.new-projects", ("New projects", "Neue Projekte"),
         ("The agent, backend and preset a new board starts with, and the board's grid.",
          "Agent, Backend und Vorlage, mit denen eine neue Tafel beginnt, und das Raster der Tafel."),
         None, "", "page-board"),
    _tip("settings.privacy", ("Privacy", "Datenschutz"),
         ("sandbox-ai's own defaults for every start: the agents' telemetry and the privacy check of pushes.",
          "Die eigenen Standards von sandbox-ai für jeden Start: Telemetrie der Agenten und die "
          "Datenschutzprüfung von Pushes."),
         ("They apply to starts from the app and from the command line.",
          "Sie gelten für Starts aus der App und von der Befehlszeile."),
         "--telemetry, --privacy-check", "privacy-check"),
    _tip("settings.policy", ("Administrator policy", "Richtlinie des Administrators"),
         ("The policy file as it is, whether it stops every start, and what it forbids. Read-only.",
          "Die Richtliniendatei, wie sie ist, ob sie jeden Start stoppt und was sie verbietet. Nur lesbar."),
         None, "/etc/sandbox-ai/policy.json", "policy"),
    # The preset gallery
    _tip("gallery.cards", ("Presets", "Vorlagen"),
         ("Ready boards by category, sandbox-ai's own presets and yours (My presets), each with a picture of its "
          "parts, its security score and what is left to fill in.",
          "Fertige Tafeln nach Kategorie, die eigenen Presets von sandbox-ai und Ihre (Meine Vorlagen), jede mit "
          "einem Bild ihrer Teile, ihrer Sicherheitspunktzahl und dem, was noch auszufüllen ist."),
         ("Select one to see it on the right; Create (or Enter, or a double-click) makes it the board.",
          "Wählen Sie eine, um sie rechts zu sehen; Anlegen (oder Enter, oder ein Doppelklick) macht sie zur Tafel."),
         "sandbox-ai presets list", "app-presets-gallery"),
    _tip("gallery.mine", ("Your own presets", "Ihre eigenen Vorlagen"),
         ("Save the board as a preset… keeps the board shown now under a name (no workspaces, no secrets). "
          "Import… adds one from a JSON file, Export… writes the selected one, Delete removes one of yours.",
          "Tafel als Vorlage speichern… bewahrt die gezeigte Tafel unter einem Namen auf (ohne Arbeitsordner, ohne "
          "Geheimnisse). Importieren… fügt eine aus einer JSON-Datei hinzu, Exportieren… schreibt die gewählte, "
          "Löschen entfernt eine Ihrer eigenen."),
         ("To edit one: create a board from it, change the board, then save it again under the same title: it "
          "replaces the old one.",
          "Zum Bearbeiten: Legen Sie daraus eine Tafel an, ändern Sie die Tafel und speichern Sie sie unter demselben"
          " Titel erneut: Sie ersetzt die alte."),
         "~/.config/sandbox-ai/app/presets.json", "app-presets-gallery"),
)


def _build():
    found = {}
    found.update(_page_tips())
    found.update(_kind_tips())
    for item in AREA_TIPS:
        assert item.id not in found, item.id
        found[item.id] = item
    return found


TIPS = _build()

# Where the window puts the (?)s of each page: (the page's attribute, tip id, how). "after": a button next to the
# control in its row; "corner": in the top right corner of the area; "group": in the title row of the group box
# around it. The board's palette, inspector and nodes, the settings' tabs, the gallery and the Help page place
# their own.
PLACES = {
    "launch": (("agent", "launch.agent", "after"), ("profile", "launch.profile", "after"),
               ("form", "launch.form", "corner"), ("preview", "launch.preview", "corner"),
               ("dry_button", "launch.dry", "after"), ("session", "launch.session", "after")),
    "board": (("view", "board.canvas", "corner"), ("findings", "board.findings", "corner"),
              ("preview", "board.command", "corner"), ("dry_button", "board.dry", "after"),
              ("apply_button", "board.apply", "after"), ("gallery_button", "board.presets", "after"),
              ("arrange_button", "board.tools", "after")),
    "sandboxes": (("table", "sandboxes.table", "corner"), ("only_project", "sandboxes.only_project", "after"),
                  ("stop_button", "sandboxes.stop", "after"), ("devices_label", "sandboxes.live", "group")),
    "approvals": (("open", "approvals.open", "group"), ("scope", "approvals.scope", "after"),
                  ("always", "approvals.always", "group")),
    "terminals": (("tabs", "terminals.tabs", "corner"),),
    "connections": (("directory", "connections.directory", "after"), ("filter", "connections.filter", "after"),
                    ("table", "connections.table", "corner"), ("detail", "connections.detail", "corner")),
    "activity": (("source", "activity.source", "after"), ("kinds_button", "activity.kinds", "after"),
                 ("table", "activity.table", "corner"), ("detail", "activity.detail", "corner")),
    "statistics": (("directory", "statistics.directory", "after"),),
    "files": (("roots", "files.roots", "after"), ("table", "files.table", "corner"),
              ("replace", "files.replace", "after")),
    "kits": (("table", "kits.table", "group"), ("use_button", "kits.use", "after"),
             ("trust_button", "kits.hooks", "after"), ("new_skill_button", "kits.skill", "after")),
    "mcp": (("servers", "mcp.servers", "group"), ("catalog", "mcp.catalog", "group")),
    "images": (("images", "images.images", "group"), ("build_button", "images.build", "after"),
               ("new_button", "images.variant", "after"), ("editor_tabs", "images.editor", "corner")),
    "credentials": (("logins", "credentials.logins", "group"), ("secrets", "credentials.secrets", "group"),
                    ("vpn", "credentials.vpn", "group")),
    "system": (("checks", "system.checks", "group"), ("theme", "system.settings", "group"),
               ("update_button", "system.update", "after"), ("policy_text", "system.policy", "group")),
    "hosts": (("list", "hosts.list", "corner"), ("tabs", "hosts.tabs", "corner")),
    "help": (),  # the Help page places its own
}
SETTINGS_TABS = ("settings.appearance", "settings.window", "settings.polling", "settings.sandbox-ai",
                 "settings.new-projects", "settings.privacy", "settings.policy")


def tip(ident):
    """The Tip of IDENT (KeyError for an unknown one: every (?) is checked by the tests)."""
    return TIPS[ident]
