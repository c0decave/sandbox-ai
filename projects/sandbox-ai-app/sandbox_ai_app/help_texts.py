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
                 "kits": "page-kits", "mcp": "page-mcp", "llm": "page-llm", "images": "page-images",
                 "credentials": "page-credentials", "team": "page-team", "system": "page-system",
                 "hosts": "page-hosts", "help": "page-help"}
PAGE_CLI = {"launch": "sandbox-ai AGENT [options]", "board": ".sandbox-ai.json",
            "sandboxes": "sandbox-ai list",
            "approvals": "--ask", "terminals": "tmux", "connections": "sandbox-ai logs, --log-dir",
            "activity": "sandbox-ai activity",
            "statistics": "sandbox-ai activity", "files": "", "kits": "sandbox-ai kits, --kit",
            "mcp": "sandbox-ai mcp, --mcp", "llm": "sandbox-ai llm, --llm NAME", "images": "sandbox-ai build",
            "credentials": "sandbox-ai secrets", "team": "sandbox-ai team",
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
    _tip("launch.host", ("Host", "Host"),
         ("Where it starts: this machine, or a host of sandbox-ai hosts (over SSH). The Model choice, the presets "
          "and the choosers of the form (VPN profiles, kits, identities, MCP servers, image variants) follow it.",
          "Wo es startet: dieser Rechner oder ein Host von sandbox-ai hosts (über SSH). Die Modellwahl, die "
          "Vorlagen und die Auswahllisten des Formulars (VPN-Profile, Kits, Identitäten, MCP-Server, "
          "Image-Varianten) folgen ihm."),
         ("On another host paths are that host's and typed in; what its sandbox-ai does not know is greyed out, "
          "and a start that needs it is refused with its names.",
          "Auf einem anderen Host sind Pfade die dieses Hosts und werden eingetippt; was sein sandbox-ai nicht "
          "kennt, ist ausgegraut, und ein Start, der es braucht, wird mit den Namen abgelehnt."),
         "sandbox-ai hosts run HOST -- AGENT", "hosts"),
    _tip("launch.model", ("Model", "Modell"),
         ("The agent's own provider (cloud), or a model server registered on the chosen host, as NAME or "
          "NAME:MODEL.",
          "Der eigene Anbieter des Agenten (Cloud) oder ein auf dem gewählten Host registrierter Modellserver, als "
          "NAME oder NAME:MODELL."),
         ("A server sets --llm and empties --model; only this server adds --no-provider-defaults. A server the "
          "agent cannot use is shown greyed out.",
          "Ein Server setzt --llm und leert --model; nur dieser Server fügt --no-provider-defaults hinzu. Ein "
          "Server, den der Agent nicht nutzen kann, wird ausgegraut gezeigt."),
         "--llm NAME[:MODEL]", "llm"),
    _tip("launch.profile", ("Profile", "Profil"),
         ("Your saved option sets for the Start page (Save as profile…): host, agent, options and arguments, "
          "loaded with Load.",
          "Ihre gespeicherten Optionssätze der Seite Start (Als Profil speichern…): Host, Agent, Optionen und "
          "Argumente, geladen mit Laden."),
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
          "Als Sitzung starten startet sie losgelöst; Verbinden, Pausieren, Fortsetzen und Stoppen auf Sandboxen & "
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
          "the selection. Drag with the right or the middle mouse button to move the view; a right click opens a "
          "menu (on a part: Properties, Cut, Copy, Paste, Remove; also Ctrl+X, Ctrl+C, Ctrl+V). Drop folders here to add them as workspace or mount, a .sandbox-ai.json to open its "
          "project, a preset file to import it. Every change is one step of Undo.",
          "Ziehen Sie einen Knoten, um ihn zu verschieben; ziehen Sie vom rechten Anschluss eines Knotens auf einen "
          "anderen, um sie zu verbinden; Entf entfernt die Auswahl. Ziehen Sie mit der rechten oder der mittleren "
          "Maustaste, um die Ansicht zu verschieben; ein Rechtsklick öffnet ein Menü (auf einem Teil: Eigenschaften, "
          "Ausschneiden, Kopieren, Einfügen, Entfernen; auch Strg+X, Strg+C, Strg+V). Legen Sie hier Ordner ab, um "
          "sie als Arbeitsordner oder Einbindung hinzuzufügen, eine .sandbox-ai.json, um ihr Projekt zu öffnen, eine "
          "Vorlagen-Datei, um sie zu importieren. Jede Änderung ist ein Schritt von Rückgängig."),
         ".sandbox-ai.json", "page-board"),
    _tip("board.inspector", ("Inspector", "Inspektor"),
         ("The settings of the selected part, built from the launcher's own options with their help texts.",
          "Die Einstellungen des gewählten Teils, aufgebaut aus den Optionen des Launchers mit ihren Hilfetexten."),
         ("A change applies to the board at once and is one step of Undo; Set up step by step… asks the "
          "questions again. It slides out at the right edge of the board when you click a part; Esc, a click on the "
          "empty board or its arrow slide it back. View > Panels > Properties as a separate window makes it a window "
          "of its own that opens when you click a part.",
          "Eine Änderung gilt sofort auf der Tafel und ist ein Schritt von Rückgängig; Schritt für Schritt "
          "einrichten… stellt die Fragen erneut. Er fährt am rechten Rand der Tafel aus, wenn Sie ein Teil "
          "anklicken; Esc, ein Klick auf die leere Tafel oder sein Pfeil fahren ihn wieder ein. Ansicht > Bereiche > "
          "Eigenschaften als eigenes Fenster macht ihn zu einem eigenen Fenster, das sich beim Klick auf ein Teil "
          "öffnet."),
         "", "page-board"),
    _tip("board.findings", ("Findings and score", "Befunde und Punktzahl"),
         ("What the launcher or the policy would refuse (✘), what makes little sense (!), and what weakens the "
          "sandbox, with a security score of 100.",
          "Was der Launcher oder die Richtlinie ablehnen würde (✘), was wenig Sinn ergibt (!) und was die Sandbox "
          "schwächt, mit einer Sicherheitspunktzahl von 100."),
         ("Shown when you ask for it: the score's button above the board, or View > Panels. Double-click a finding "
          "to select its part. Apply and Start refuse while there is an error.",
          "Wird gezeigt, wenn Sie danach fragen: die Schaltfläche des Sicherheitswerts über der Tafel oder Ansicht > "
          "Bereiche. Doppelklicken Sie einen Befund, um sein Teil zu wählen. Übernehmen und Starten verweigern, "
          "solange es einen Fehler gibt."),
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
          "Neu aus Vorlage… öffnet die Galerie: fertige Tafeln, die Vorlagen von sandbox-ai und Ihre eigenen Vorlagen."
          " Assistent… baut ein neues Projekt Schritt für Schritt."),
         ("In the gallery, Save the board as a preset… keeps the board shown now as your own preset.",
          "In der Galerie speichert Tafel als Vorlage speichern… die gezeigte Tafel als Ihre eigene Vorlage."),
         "sandbox-ai presets list", "app-presets-gallery"),
    _tip("board.tools", ("Undo, arrange, zoom", "Rückgängig, Anordnen, Zoom"),
         ("Undo and Redo go through every change of this project's board; Arrange lays the parts out in columns; "
          "+, − and Fit zoom.",
          "Rückgängig und Wiederholen gehen durch jede Änderung an der Tafel dieses Projekts; Anordnen legt die Teile"
          " in Spalten; +, − und Einpassen zoomen."),
         ("Ctrl and the mouse wheel zoom too. Drag with the right or the middle mouse button to move the view; a "
          "right click opens a menu (add a part there, select all, fit, zoom 100 %, or the node's own actions).",
          "Strg und das Mausrad zoomen ebenfalls. Ziehen Sie mit der rechten oder der mittleren Maustaste, um die "
          "Ansicht zu verschieben; ein Rechtsklick öffnet ein Menü (ein Teil dort hinzufügen, alles auswählen, "
          "einpassen, Zoom 100 % oder die Aktionen des Knotens)."),
         "", "page-board"),
    # Sandboxes & sessions
    _tip("sandboxes.table", ("Sandboxes, sessions, MCP servers", "Sandboxen, Sitzungen, MCP-Server"),
         ("Everything that runs or is kept on this computer and on every host, as a tree: each host (how many "
          "starts, how many run, whether it answers), under it its starts with kind, project, agent, backend, "
          "start time and host, and under each start what belongs to it: the MCP servers and helpers it reaches, "
          "its Podman pod; under an MCP server the starts it serves. Rows of the current project are bold, a start "
          "with an open question is marked asking. Rows of a host that did not answer are greyed (last seen N s "
          "ago) and cannot be acted on.",
          "Alles, was auf diesem Rechner und auf jedem Host läuft oder aufbewahrt wird, als Baum: jeder Host (wie "
          "viele Starts, wie viele laufen, ob er antwortet), darunter seine Starts mit Art, Projekt, Agent, "
          "Backend, Startzeit und Host, und unter jedem Start, was zu ihm gehört: die MCP-Server und Helfer, die er "
          "erreicht, sein Podman-Pod; unter einem MCP-Server die Starts, die er bedient. Zeilen des aktuellen "
          "Projekts sind fett, ein Start mit offener Frage ist als fragt markiert. Zeilen eines Hosts, der nicht "
          "antwortete, sind ausgegraut (zuletzt vor N s gesehen) und lassen sich nicht bedienen."),
         ("Select a start for its details and the buttons that act on it; a host's row and what hangs below a "
          "start have no buttons (a double-click opens the row it stands for). Which groups are open is kept; "
          "View › Fold all sections and Unfold all sections close or open them all. The hosts are read about "
          "every 10 seconds while the page is shown, every minute otherwise.",
          "Wählen Sie einen Start für seine Details und die Knöpfe, die auf ihn wirken; die Zeile eines Hosts und "
          "was unter einem Start hängt, haben keine Knöpfe (ein Doppelklick öffnet die Zeile, für die es steht). "
          "Welche Gruppen offen sind, bleibt erhalten; Ansicht › Alle Abschnitte einklappen und Alle Abschnitte "
          "ausklappen schließen oder öffnen alle. Die Hosts werden etwa alle 10 Sekunden gelesen, solange die "
          "Seite zu sehen ist, sonst jede Minute."),
         "sandbox-ai list", "sessions"),
    _tip("sandboxes.only_project", ("Only the current project", "Nur das aktuelle Projekt"),
         ("Limits the list to the sandboxes and sessions of the project open now.",
          "Beschränkt die Liste auf die Sandboxen und Sitzungen des gerade offenen Projekts."),
         None, "", "page-sandboxes"),
    _tip("sandboxes.ended", ("Ended", "Beendete"),
         ("Also lists the starts that ended: stopped MCP servers and whatever else sandbox-ai keeps, on this "
          "computer and every host.",
          "Listet auch die beendeten Starts: gestoppte MCP-Server und was sandbox-ai sonst noch aufbewahrt, auf "
          "diesem Rechner und jedem Host."),
         ("History… shows what happened to the selected start; Clean up… removes leftovers and kept starts "
          "unused for longer than keep_days, after showing what it would remove.",
          "Verlauf… zeigt, was mit dem gewählten Start geschah; Aufräumen… entfernt Reste und aufbewahrte Starts, "
          "die länger als keep_days ungenutzt sind, nachdem es gezeigt hat, was es entfernen würde."),
         "sandbox-ai list --all, sandbox-ai gc", "page-sandboxes"),
    _tip("sandboxes.hosts", ("Host", "Host"),
         ("Whose starts are listed: all of them, this machine's or one host's of sandbox-ai hosts. Shown once "
          "there are hosts; Show starts on the Hosts page sets it.",
          "Wessen Starts aufgelistet sind: alle, die dieses Rechners oder die eines Hosts von sandbox-ai hosts. "
          "Zu sehen, sobald es Hosts gibt; Starts zeigen auf der Seite Hosts stellt es ein."),
         ("Each host is read on its own: one that hangs holds up no other, and its rows stay greyed with when it "
          "answered last. Clean up… acts on the host chosen here.",
          "Jeder Host wird für sich gelesen: Einer, der hängt, hält keinen anderen auf, und seine Zeilen bleiben "
          "ausgegraut mit der Zeit seiner letzten Antwort. Aufräumen… wirkt auf den hier gewählten Host."),
         "sandbox-ai hosts run HOST -- list --json", "page-hosts"),
    _tip("sandboxes.manage", ("Rename, History, Clean up", "Umbenennen, Verlauf, Aufräumen"),
         ("Rename… gives the selected start another name: its run id stays, and so do its row and its tab. "
          "History… shows its starts, ends, pauses and renames. Clean up… removes leftovers and kept starts unused "
          "for longer than keep_days.",
          "Umbenennen… gibt dem gewählten Start einen anderen Namen: Seine Lauf-ID bleibt, und mit ihr seine Zeile "
          "und sein Tab. Verlauf… zeigt seine Starts, Enden, Pausen und Umbenennungen. Aufräumen… entfernt Reste "
          "und aufbewahrte Starts, die länger als keep_days ungenutzt sind."),
         ("Clean up shows what it would remove and asks; it removes exactly that, or nothing when the list "
          "changed meanwhile. They wait while a command runs.",
          "Aufräumen zeigt, was es entfernen würde, und fragt; es entfernt genau das, oder nichts, wenn sich die "
          "Liste inzwischen geändert hat. Sie warten, solange ein Befehl läuft."),
         "sandbox-ai rename OLD NEW, sandbox-ai list --history NAME, sandbox-ai gc --dry-run", "page-sandboxes"),
    _tip("sandboxes.stop", ("Stop", "Stoppen"),
         ("Ends the selected sandbox after asking; its launcher cleans up. A session can be paused instead.",
          "Stoppt die gewählte Sandbox nach Rückfrage; ihr Launcher räumt auf. Eine Sitzung lässt sich "
          "stattdessen pausieren."),
         None, "sandbox-ai stop NAME", "sessions"),
    _tip("sandboxes.live", ("While it runs", "Während sie läuft"),
         ("Changes for a running sandbox: allow or revoke a host, hand over a device, change the limits, show "
          "its log, add or remove a workspace (a folder, a single file, with a name of its own) and add or remove "
          "an MCP server or a helper agent without a restart.",
          "Änderungen für eine laufende Sandbox: einen Host erlauben oder entziehen, ein Gerät geben, die Grenzen "
          "ändern, ihr Protokoll zeigen, einen Arbeitsordner (einen Ordner, eine einzelne Datei, mit eigenem Namen) "
          "hinzufügen oder entfernen und einen MCP-Server oder einen Helfer-Agenten ohne Neustart hinzufügen oder "
          "entfernen."),
         ("Allowed hosts, devices and limits last until the sandbox ends; a session keeps an added folder for its "
          "restart unless it is read-only. An MCP server added here is kept for the start's later starts too "
          "(Remove MCP server… takes it from them); where the agent needs a restart to see it, the app offers one "
          "(the agent continues its conversation). Each widening asks first; the project file stays as it is.",
          "Erlaubte Hosts, Geräte und Grenzen gelten, bis die Sandbox endet; eine Sitzung behält einen "
          "hinzugefügten Ordner für ihren Neustart, außer er ist nur lesbar. Ein hier hinzugefügter MCP-Server "
          "bleibt auch für die späteren Starts des Starts (MCP-Server entfernen… nimmt ihn ihnen); braucht der "
          "Agent einen Neustart, um ihn zu sehen, bietet die App einen an (der Agent setzt sein Gespräch fort). "
          "Jede Erweiterung fragt zuerst; die Projektdatei bleibt, wie sie ist."),
         "sandbox-ai net allow | devices | limits | workspace add | mcp attach --live | mcp detach --live",
         "page-sandboxes"),
    # Approvals
    _tip("approvals.open", ("Open questions", "Offene Fragen"),
         ("What running sandboxes ask, on this computer and on every host: a destination not allowed yet, a push "
          "with warnings, an MCP tool call, a page to open.",
          "Was laufende Sandboxen fragen, auf diesem Rechner und auf jedem Host: ein noch nicht erlaubtes Ziel, ein "
          "Push mit Warnungen, ein MCP-Werkzeugaufruf, eine zu öffnende Seite."),
         ("The sandbox waits for your answer; without one in time the request is refused. Each question is "
          "answered on its own: a slow host never holds up the others.",
          "Die Sandbox wartet auf Ihre Antwort; ohne Antwort rechtzeitig wird die Anfrage abgelehnt. Jede Frage "
          "wird für sich beantwortet: Ein langsamer Host hält die anderen nie auf."),
         "--ask, sandbox-ai approvals list", "approvals"),
    _tip("approvals.hosts", ("Questions of", "Fragen von"),
         ("Whose questions are listed: every host's, this machine's or one host's. Show questions on the Hosts "
          "page sets it.",
          "Wessen Fragen aufgelistet sind: die aller Hosts, die dieses Rechners oder die eines Hosts. Fragen "
          "zeigen auf der Seite Hosts stellt es ein."),
         None, "sandbox-ai hosts run HOST -- approvals list", "page-approvals"),
    _tip("approvals.scope", ("For how long", "Für wie lange"),
         ("How long an allow lasts: once, for this run, or always (kept for the project). The security mode and "
          "the administrator decide what is offered.",
          "Wie lange ein Erlauben gilt: einmal, für diesen Lauf oder immer (für das Projekt gemerkt). "
          "Sicherheitsmodus und Administrator bestimmen, was angeboten wird."),
         ("Allow (Ctrl+Return) uses the scope chosen here. Deny for this run (Ctrl+Shift+D) refuses it until the "
          "run ends, Deny once only this time, Block always… (Ctrl+Shift+K, asked first) at every later start of "
          "the project.",
          "Erlauben (Strg+Eingabe) nutzt die hier gewählte Geltung. Für diesen Lauf ablehnen (Strg+Umschalt+D) "
          "weist es bis zum Ende des Laufs ab, Einmal ablehnen nur dieses Mal, Immer sperren… (Strg+Umschalt+K, "
          "mit Rückfrage) bei jedem späteren Start des Projekts."),
         "sandbox-ai approvals allow | deny ID --scope once|run|always", "approvals"),
    _tip("approvals.always", ("Allowed or blocked always", "Immer erlaubt oder blockiert"),
         ("Your standing answers per project, of this machine or of a host chosen beside Refresh.",
          "Ihre dauerhaften Antworten pro Projekt, die dieses Rechners oder eines Hosts, den Sie neben "
          "Neu laden wählen."),
         ("Forget… one, there too, and the sandbox asks again the next time.",
          "Vergessen Sie eine mit Vergessen…, auch dort, fragt die Sandbox beim nächsten Mal wieder."),
         "sandbox-ai approvals remembered, sandbox-ai approvals forget", "approvals"),
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
    _tip("connections.directory", ("Log folder", "Protokollordner"),
         ("Which logs are read: the project's log folder, one of an earlier run, or one you choose.",
          "Welche Protokolle gelesen werden: der Protokollordner des Projekts, einer eines früheren Laufs oder einer Ihrer "
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
          "Der Protokollordner, dessen Läufe gezählt werden: Tokens, Anfragen und Werkzeugaufrufe pro Lauf und pro "
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
         ("The left pane: the files of the chosen sandbox folder; the right pane: this machine. Links are shown "
          "with where they point but never followed.",
          "Der linke Bereich: die Dateien des gewählten Sandbox-Ordners; der rechte Bereich: dieser Rechner. Links "
          "werden mit ihrem Ziel gezeigt, aber nie verfolgt."),
         ("Copy (F5) or Move (F6) what is selected in one pane into the other pane's folder: no mount is needed. "
          "Tab switches panes.",
          "Kopieren (F5) oder Verschieben (F6) bringt das in einem Bereich Gewählte in den Ordner des anderen "
          "Bereichs: Es braucht keine Einbindung. Tab wechselt den Bereich."),
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
    # LLM servers
    _tip("llm.host", ("Whose servers", "Wessen Server"),
         ("This machine or a host of sandbox-ai hosts: the list, Add, Check, Models and Remove act there (on a "
          "host over SSH).",
          "Dieser Rechner oder ein Host von sandbox-ai hosts: Liste, Hinzufügen, Prüfen, Modelle und Entfernen "
          "wirken dort (auf einem Host über SSH)."),
         None, "sandbox-ai hosts run HOST -- llm list", "page-llm"),
    _tip("llm.servers", ("Your LLM servers", "Ihre LLM-Server"),
         ("The model servers registered by name (Ollama, LM Studio, llama.cpp, vLLM or another OpenAI-compatible "
          "server), local or reached over a VPN, with kind, URL, model, key and the agents they suit.",
          "Die unter einem Namen eingetragenen Modellserver (Ollama, LM Studio, llama.cpp, vLLM oder ein anderer "
          "OpenAI-kompatibler Server), lokal oder über ein VPN erreicht, mit Art, URL, Modell, Schlüssel und den "
          "Agenten, zu denen sie passen."),
         ("A start uses one with --llm NAME[:MODEL]: only its address and port are opened, and the agent sees a "
          "placeholder instead of the key.",
          "Ein Start nutzt einen mit --llm NAME[:MODELL]: Nur seine Adresse und sein Port werden geöffnet, und der "
          "Agent sieht einen Platzhalter statt des Schlüssels."),
         "sandbox-ai llm list, --llm NAME", "llm"),
    _tip("llm.add", ("Add a server", "Einen Server hinzufügen"),
         ("Registers a server under a name: its kind, URL, an optional default model, VPN profile and key.",
          "Trägt einen Server unter einem Namen ein: seine Art, URL und optional ein Standardmodell, ein "
          "VPN-Profil und einen Schlüssel."),
         ("A key typed here is kept as a secret of sandbox-ai, never on a command line; on a host, name a secret "
          "kept there already.",
          "Ein hier eingegebener Schlüssel wird als Geheimnis von sandbox-ai aufbewahrt, nie auf einer "
          "Befehlszeile; auf einem Host nennen Sie ein dort schon aufbewahrtes Geheimnis."),
         "sandbox-ai llm add NAME --kind KIND --url URL", "page-llm"),
    _tip("llm.check", ("Check and models", "Prüfen und Modelle"),
         ("Check tests reachability, TLS, the key, the model and the API the chosen agent needs; Models lists "
          "what the server offers now.",
          "Prüfen testet Erreichbarkeit, TLS, den Schlüssel, das Modell und die API, die der gewählte Agent "
          "braucht; Modelle listet, was der Server gerade anbietet."),
         ("Nothing is changed; Remove… forgets a server after asking (its secret stays).",
          "Nichts wird geändert; Entfernen… vergisst einen Server nach Rückfrage (sein Geheimnis bleibt)."),
         "sandbox-ai llm check NAME --agent AGENT, sandbox-ai llm models NAME", "llm"),
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
    _tip("credentials.overview", ("Everything configured", "Alles Eingerichtete"),
         ("Agent logins, secrets, Git and publish logins, identities and VPN profiles in one list, each with its "
          "state; what needs attention is marked.",
          "Logins der Agenten, Geheimnisse, Git- und Veröffentlichungs-Logins, Identitäten und VPN-Profile in einer "
          "Liste, jedes mit seinem Zustand; was Aufmerksamkeit braucht, ist markiert."),
         ("A click on a row opens its tab, where it can be changed. No value of a secret is ever shown.",
          "Ein Klick auf eine Zeile öffnet ihren Reiter, wo es sich ändern lässt. Kein Wert eines Geheimnisses wird "
          "je gezeigt."),
         "sandbox-ai secrets | git-logins | publish-logins | identities | vpn", "secrets"),
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
    # Teams
    _tip("team.host", ("Whose teams", "Wessen Teams"),
         ("This machine or a host of sandbox-ai hosts: the list, Start, Post, Stop and Delete act there (on a host "
          "over SSH).",
          "Dieser Rechner oder ein Host von sandbox-ai hosts: Liste, Starten, Aufgabe, Stoppen und Löschen wirken "
          "dort (auf einem Host über SSH)."),
         None, "sandbox-ai hosts run HOST -- team list", "page-team"),
    _tip("team.teams", ("The teams", "Die Teams"),
         ("Each team with its members and its shared workspace; every member is a session of its own "
          "(NAME-AGENT).",
          "Jedes Team mit seinen Mitgliedern und seinem gemeinsamen Arbeitsordner; jedes Mitglied ist eine eigene "
          "Sitzung (NAME-AGENT)."),
         None, "sandbox-ai team list", "teams"),
    _tip("team.board", ("The board", "Die Tafel"),
         ("The tasks between the members: who posted each, for whom, its state and what it asks. The board is an "
          "MCP server of the team, in a sandbox without network.",
          "Die Aufgaben zwischen den Mitgliedern: wer sie gestellt hat, für wen, ihr Stand und was sie verlangt. "
          "Die Tafel ist ein MCP-Server des Teams, in einer Sandbox ohne Netzwerk."),
         ("Post a task… puts one there from you.", "Aufgabe stellen… legt eine von Ihnen dort ab."),
         "sandbox-ai team show NAME, team post NAME AGENT TASK", "teams"),
    _tip("team.start", ("Start a team", "Ein Team starten"),
         ("Two or three agents on one workspace; the first leads. The members start in a terminal, one after "
          "the other.",
          "Zwei oder drei Agenten auf einem Arbeitsordner; der erste leitet. Die Mitglieder starten in einem "
          "Terminal, einer nach dem anderen."),
         ("Talk to the lead afterwards: it hands the others their tasks.",
          "Sprechen Sie danach mit der Leitung: Sie verteilt die Aufgaben an die anderen."),
         "sandbox-ai team start NAME --agents claude,codex -w DIR", "page-team"),
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
          "Farbschema, Sprache, System-Tray und Terminal; Alle Einstellungen… hat den Rest."),
         ("The theme applies at once, a new language at the next start.",
          "Das Farbschema gilt sofort, eine neue Sprache beim nächsten Start."),
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
    _tip("hosts.table", ("Hosts", "Hosts"),
         ("Other machines whose sandbox-ai this app manages over SSH, such as LXC containers: for each, whether it "
          "answered (or when it was last seen), its sandbox-ai version, the starts running and the questions "
          "waiting there.",
          "Andere Rechner, deren sandbox-ai diese App über SSH verwaltet, etwa LXC-Container: für jeden, ob er "
          "antwortete (oder wann er zuletzt gesehen wurde), seine sandbox-ai-Version, die dort laufenden Starts "
          "und die wartenden Fragen."),
         ("Add…, Edit… (whether its update uses sudo), Remove… and Test act on the list; Update and Update all "
          "run each host's own signed update.",
          "Hinzufügen…, Bearbeiten… (ob sein Update sudo nutzt), Entfernen… und Testen wirken auf die Liste; "
          "Aktualisieren und Alle aktualisieren führen das eigene signierte Update jedes Hosts aus."),
         "sandbox-ai hosts list | add | set | remove | test | update", "hosts"),
    _tip("hosts.show", ("Its starts and questions", "Seine Starts und Fragen"),
         ("Show starts opens Sandboxes & sessions with only this host's starts, Show questions the Approvals page "
          "with only its questions.",
          "Starts zeigen öffnet Sandboxen & Sitzungen nur mit den Starts dieses Hosts, Fragen zeigen die Seite "
          "Freigaben nur mit seinen Fragen."),
         ("Attach, pause, stop, allow and deny happen there, as for this computer.",
          "Verbinden, Pausieren, Stoppen, Erlauben und Ablehnen geschehen dort, wie für diesen Rechner."),
         "sandbox-ai hosts run HOST -- list", "page-hosts"),
    _tip("hosts.tabs", ("On the host", "Auf dem Host"),
         ("The chosen host's doctor, its updates, and its defaults for every start there: security mode, "
          "canaries, how long kept starts stay, the agents' telemetry.",
          "Der doctor des gewählten Hosts, seine Updates und seine Standards für jeden Start dort: "
          "Sicherheitsmodus, Canaries, wie lange aufbewahrte Starts bleiben, die Telemetrie der Agenten."),
         ("Save defaults… writes them there; a step down in security is asked first.",
          "Vorgaben speichern… schreibt sie dort; ein Schritt zu weniger Sicherheit wird vorher erfragt."),
         "sandbox-ai defaults set | unset", "page-hosts"),
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
          "Farbschema und Sprache, der Guide für neue Teile der Tafel, die geführte Tour und das Intro."),
         ("Each field is saved when it changes; a new language applies at the next start.",
          "Jedes Feld wird gespeichert, wenn es sich ändert; eine neue Sprache gilt beim nächsten Start."),
         "", "app-settings"),
    _tip("settings.window", ("Window", "Fenster"),
         ("The system tray, start at login, where commands run, Chromium's sandbox for terminal tabs, "
          "notifications, tabs for new sessions, debug logging.",
          "System-Tray, Start bei der Anmeldung, wo Befehle laufen, Chromiums Sandbox für Terminal-Tabs, "
          "Benachrichtigungen, Tabs für neue Sitzungen, Debug-Protokoll."),
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
          "Fertige Tafeln nach Kategorie, die eigenen Vorlagen von sandbox-ai und Ihre (Meine Vorlagen), jede mit "
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
    "launch": (("host_choice", "launch.host", "after"), ("agent", "launch.agent", "after"),
               ("model_choice", "launch.model", "after"), ("profile", "launch.profile", "after"),
               ("form", "launch.form", "corner"), ("preview", "launch.preview", "corner"),
               ("dry_button", "launch.dry", "after"), ("session", "launch.session", "after")),
    "board": (("view", "board.canvas", "corner"), ("findings", "board.findings", "corner"),
              ("preview", "board.command", "corner"), ("dry_button", "board.dry", "after"),
              ("apply_button", "board.apply", "after"), ("gallery_button", "board.presets", "after"),
              ("arrange_button", "board.tools", "after")),
    "sandboxes": (("table", "sandboxes.table", "corner"), ("only_project", "sandboxes.only_project", "after"),
                  ("show_ended", "sandboxes.ended", "after"), ("host_filter", "sandboxes.hosts", "after"),
                  ("clean_button", "sandboxes.manage", "after"),
                  ("stop_button", "sandboxes.stop", "after"), ("devices_label", "sandboxes.live", "group")),
    "approvals": (("open", "approvals.open", "group"), ("host_filter", "approvals.hosts", "after"),
                  ("scope", "approvals.scope", "after"),
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
    "llm": (("host_box", "llm.host", "after"), ("servers", "llm.servers", "corner"),
            ("add_button", "llm.add", "after"), ("check_button", "llm.check", "after")),
    "images": (("images", "images.images", "group"), ("build_button", "images.build", "after"),
               ("new_button", "images.variant", "after"), ("editor_tabs", "images.editor", "corner")),
    "credentials": (("overview", "credentials.overview", "group"), ("logins", "credentials.logins", "group"),
                    ("secrets", "credentials.secrets", "group"), ("vpn", "credentials.vpn", "group")),
    "team": (("hosts", "team.host", "after"), ("table", "team.teams", "corner"),
             ("board", "team.board", "corner"), ("start_button", "team.start", "after")),
    "system": (("checks", "system.checks", "group"), ("theme", "system.settings", "group"),
               ("update_button", "system.update", "after"), ("policy_text", "system.policy", "group")),
    "hosts": (("table", "hosts.table", "corner"), ("starts_button", "hosts.show", "after"),
              ("tabs", "hosts.tabs", "corner")),
    "help": (),  # the Help page places its own
}
SETTINGS_TABS = ("settings.appearance", "settings.window", "settings.polling", "settings.sandbox-ai",
                 "settings.new-projects", "settings.privacy", "settings.policy")


def tip(ident):
    """The Tip of IDENT (KeyError for an unknown one: every (?) is checked by the tests)."""
    return TIPS[ident]


# -- short explanations on hover: they stay when the (?)s are switched off --
# Column titles and form labels with a short explanation, by their English words (the tr() key of the label): a
# table header or a form label showing these words, in either language, gets the explanation as its tooltip
# (help_view.explain_tree). Only words that mean the same in every table and form of the app are listed.

def _terms(*pairs):
    return {key: Text(en, de) for key, en, de in pairs}


TERMS = _terms(
    # Approvals
    ("Question", "What the sandbox asks to do.", "Was die Sandbox tun möchte."),
    ("Asked", "When the sandbox asked.", "Wann die Sandbox gefragt hat."),
    ("Refused in", "How long the sandbox still waits for your answer; then the request is refused.",
     "Wie lange die Sandbox noch auf Ihre Antwort wartet; danach wird die Anfrage abgelehnt."),
    ("Answer", "What was answered: allowed or blocked.", "Was geantwortet wurde: erlaubt oder blockiert."),
    ("Scope", "How long an answer holds: once, for this run, or always for the project.",
     "Wie lange eine Antwort gilt: einmal, für diesen Lauf oder immer für das Projekt."),
    ("Allowed", "What is allowed or blocked for good in this project: a host, an address, a tool.",
     "Was in diesem Projekt dauerhaft erlaubt oder blockiert ist: ein Host, eine Adresse, ein Werkzeug."),
    # Sandboxes, connections, files
    ("Backend", "What isolates the sandbox: Podman (a container) or Bubblewrap (namespaces, no service).",
     "Was die Sandbox abschottet: Podman (ein Container) oder Bubblewrap (Namespaces, ohne Dienst)."),
    ("Proxy", "Whether the connection proxy checks this start's network traffic.",
     "Ob der Verbindungs-Proxy den Netzverkehr dieses Starts prüft."),
    ("Method", "The HTTP method of the request (GET, POST …), or CONNECT for a tunnel.",
     "Die HTTP-Methode der Anfrage (GET, POST …) oder CONNECT für einen Tunnel."),
    ("Methods", "The HTTP methods allowed for this host and path.",
     "Die HTTP-Methoden, die für diesen Host und Pfad erlaubt sind."),
    ("Target", "Where the connection went: host and port, or the URL.",
     "Wohin die Verbindung ging: Host und Port oder die URL."),
    ("Bytes", "How much data went through.", "Wie viele Daten übertragen wurden."),
    ("Points to (not followed)", "Where a link points; the app shows it but never follows it.",
     "Wohin ein Link zeigt; die App zeigt es, folgt ihm aber nie."),
    ("Owner", "The user and group the file belongs to.", "Benutzer und Gruppe, denen die Datei gehört."),
    ("Workspace", "The project folder, mounted at /work.", "Der Projektordner, eingebunden unter /work."),
    # Kits
    ("Brings", "What the kit brings into the sandbox HOME: skills, agents, commands, hooks, plugins.",
     "Was das Kit ins Sandbox-HOME mitbringt: Skills, Agenten, Befehle, Hooks, Plugins."),
    ("This project", "Whether the current project uses this kit (--kit in its .sandbox-ai.json).",
     "Ob das aktuelle Projekt dieses Kit nutzt (--kit in seiner .sandbox-ai.json)."),
    ("Marketplace", "A plugin marketplace the agent knows: where its plugins come from.",
     "Ein Plugin-Marktplatz, den der Agent kennt: woher seine Plugins kommen."),
    ("This kit", "Whether the plugin is in the chosen kit.", "Ob das Plugin im gewählten Kit ist."),
    ("Marketplace source", "Where the marketplace comes from: a GitHub repository (owner/repo), a Git URL or a "
     "folder.", "Woher der Marktplatz kommt: ein GitHub-Repository (owner/repo), eine Git-URL oder ein Ordner."),
    # MCP servers
    ("Serves", "The agent starts this server is connected to.",
     "Die Agent-Starts, mit denen dieser Server verbunden ist."),
    ("Runs", "What the server runs: its image or its command.", "Was der Server ausführt: sein Image oder seinen Befehl."),
    ("Catalog", "The catalog the server comes from (mcp: sandbox-ai-mcp, hub: axross-mcp-hub).",
     "Der Katalog, aus dem der Server stammt (mcp: sandbox-ai-mcp, hub: axross-mcp-hub)."),
    ("Entry", "The catalog's name for the server; adding it defines it under a name of yours.",
     "Der Name des Servers im Katalog; Hinzufügen definiert ihn unter einem Namen Ihrer Wahl."),
    ("Network", "The network the server gets: none, or only the hosts it needs.",
     "Das Netz, das der Server bekommt: keines oder nur die Hosts, die er braucht."),
    ("Published", "When the catalog published this entry.", "Wann der Katalog diesen Eintrag veröffentlicht hat."),
    ("Signature", "Whether the entry's signature was checked against the catalog's key.",
     "Ob die Signatur des Eintrags gegen den Schlüssel des Katalogs geprüft wurde."),
    ("Name for --mcp", "The name a start uses for this server: --mcp NAME.",
     "Der Name, mit dem ein Start diesen Server nutzt: --mcp NAME."),
    ("Folders", "Folders of the host the server may read or write; nothing else of your files.",
     "Ordner des Hosts, die der Server lesen oder schreiben darf; sonst nichts von Ihren Dateien."),
    ("Variables", "Environment variables set for the server.", "Umgebungsvariablen, die für den Server gesetzt werden."),
    ("Variables from your environment", "Variables copied by name from your own environment.",
     "Variablen, die per Name aus Ihrer eigenen Umgebung übernommen werden."),
    ("Processes", "The most processes it may run at once.", "Wie viele Prozesse es höchstens gleichzeitig ausführen darf."),
    ("VPN", "The VPN profile it is reached through, if any.", "Das VPN-Profil, über das es erreicht wird, falls eines."),
    # LLM servers
    ("Agents", "The agents it is for.", "Die Agenten, für die es gedacht ist."),
    ("Default model", "The model a start uses when it names only the server (--llm NAME).",
     "Das Modell, das ein Start nutzt, wenn er nur den Server nennt (--llm NAME)."),
    ("Secret for the API key (optional)", "The name of a secret that holds the key; the key never enters a sandbox.",
     "Der Name eines Geheimnisses mit dem Schlüssel; der Schlüssel kommt nie in eine Sandbox."),
    ("Headers from secrets", "Further HTTP headers whose values come from secrets, such as an organization ID.",
     "Weitere HTTP-Header, deren Werte aus Geheimnissen kommen, etwa eine Organisations-ID."),
    ("CA certificate", "The certificate authority of a server with a TLS certificate of its own.",
     "Die Zertifizierungsstelle eines Servers mit eigenem TLS-Zertifikat."),
    ("Loaded", "Whether the server holds the model in memory now.", "Ob der Server das Modell gerade im Speicher hält."),
    ("Params", "The model's size in parameters (7B: seven billion).",
     "Die Größe des Modells in Parametern (7B: sieben Milliarden)."),
    ("Quant", "How coarsely the weights are stored: Q4 is smaller and faster, Q8 closer to the original.",
     "Wie grob die Gewichte gespeichert sind: Q4 ist kleiner und schneller, Q8 näher am Original."),
    ("Context", "How many tokens the model reads at once.", "Wie viele Tokens das Modell auf einmal liest."),
    # VPN profiles
    ("Provider", "The VPN service of the profile, if it uses one (Mullvad, Tailscale …).",
     "Der VPN-Dienst des Profils, falls es einen nutzt (Mullvad, Tailscale …)."),
    ("Resolvers", "The DNS servers the sandbox asks behind the VPN.",
     "Die DNS-Server, die die Sandbox hinter dem VPN fragt."),
    ("Check URLs", "Services that echo the address traffic leaves from: the start checks that it is the VPN's.",
     "Dienste, die die Adresse nennen, von der der Verkehr ausgeht: Der Start prüft, dass es die des VPN ist."),
    # Identities and credentials
    ("Commits as", "The name and e-mail address in the agent's commits.",
     "Der Name und die E-Mail-Adresse in den Commits des Agenten."),
    ("Git servers", "The Git servers this identity logs in to, through the connection proxy.",
     "Die Git-Server, bei denen sich diese Identität über den Verbindungs-Proxy anmeldet."),
    ("Publishes to", "The registries and forges it may publish to (--publish).",
     "Die Registries und Forges, in die sie veröffentlichen darf (--publish)."),
    ("Git server", "The Git host the login is for, such as github.com.",
     "Der Git-Host, für den die Anmeldung gilt, etwa github.com."),
    ("Secret", "The name of the secret (sandbox-ai secrets) that holds the key or token.",
     "Der Name des Geheimnisses (sandbox-ai secrets), das den Schlüssel oder Token enthält."),
    ("Kept in", "Where the secret is stored: your keyring or a private file.",
     "Wo das Geheimnis liegt: in Ihrem Schlüsselbund oder in einer privaten Datei."),
    ("Used by", "The starts, servers and logins that name this secret.",
     "Die Starts, Server und Anmeldungen, die dieses Geheimnis nennen."),
    ("Where / used by", "Where it is kept, and which starts or servers use it.",
     "Wo es aufbewahrt wird und welche Starts oder Server es nutzen."),
    # Hosts
    ("SSH", "The SSH address the app reaches the host at.", "Die SSH-Adresse, unter der die App den Host erreicht."),
    ("Answers", "Whether the host answered the last time, or when it was last seen.",
     "Ob der Host zuletzt geantwortet hat, oder wann er zuletzt gesehen wurde."),
    ("Running", "How many starts run there now.", "Wie viele Starts dort gerade laufen."),
    ("Questions", "How many questions of its sandboxes wait for an answer.",
     "Wie viele Fragen seiner Sandboxen auf eine Antwort warten."),
    # Images
    ("Built", "Whether the image exists in Podman.", "Ob das Image in Podman vorhanden ist."),
    ("Extension", "Whether your extension (packages and files on top of the base image) changed since the build.",
     "Ob sich Ihre Erweiterung (Pakete und Dateien über dem Basis-Image) seit dem Bau geändert hat."),
    ("Agent version", "The version of the agent inside the image.", "Die Version des Agenten im Image."),
    ("Path in the image", "Where the file lands in the image.", "Wohin die Datei im Image kommt."),
    # Teams
    ("Members", "The agents of the team; the first one leads.", "Die Agenten des Teams; der erste leitet."),
    ("Role", "Lead or member: the lead hands the others tasks.",
     "Leitung oder Mitglied: Die Leitung gibt den anderen Aufgaben."),
    # Privacy check and canaries
    ("Severity", "How serious the finding is.", "Wie ernst der Fund ist."),
    ("Rule", "The privacy rule that matched.", "Die Datenschutzregel, die angeschlagen hat."),
    ("Canary", "The canary (a decoy file with a token) that was touched.",
     "Der Canary (eine Köderdatei mit Token), der angefasst wurde."),
    # Settings
    ("Preset directories", "Folders with presets of your own; the gallery shows them too.",
     "Ordner mit eigenen Vorlagen; die Galerie zeigt sie auch."),
    ("Trusted projects", "Projects whose .sandbox-ai.json you trusted; a changed file is asked about again.",
     "Projekte, deren .sandbox-ai.json Sie vertraut haben; nach einer Änderung wird wieder gefragt."),
)

# The settings of the Settings dialog, by their key (backend.SETTINGS).
SETTING_TIPS = _terms(
    ("theme", "The app's colours and shapes.", "Farben und Formen der App."),
    ("language", "The language of the app; it applies at the next start.",
     "Die Sprache der App; sie gilt ab dem nächsten Start."),
    ("guide_nodes", "A new part of the board asks what matters for it, one question at a time.",
     "Ein neues Teil der Tafel fragt nacheinander, worauf es ankommt."),
    ("guide_tour", "At the first start the app offers a tour of its pages.",
     "Beim ersten Start bietet die App eine Tour durch ihre Seiten an."),
    ("help_buttons", "Off hides every (?); these short explanations on hover, the Help page, the tours and F1 stay.",
     "Aus blendet jedes (?) aus; diese kurzen Erklärungen beim Zeigen, die Hilfe, die Touren und F1 bleiben."),
    ("intro", "A short animation when the app starts; Esc skips it.",
     "Eine kurze Animation beim Start der App; Esc überspringt sie."),
    ("tray", "Closing the window keeps the app in the system tray, so questions are still answered.",
     "Das Schließen des Fensters lässt die App im System-Tray, damit Fragen weiter beantwortet werden."),
    ("autostart", "The app starts in the tray when you log in.", "Die App startet bei Ihrer Anmeldung im Tray."),
    ("terminal", "Where builds, logins and other commands run: in a tab of the app or in a terminal window.",
     "Wo Builds, Anmeldungen und andere Befehle laufen: in einem Tab der App oder in einem Terminalfenster."),
    ("web_without_sandbox", "Terminal tabs work without free user namespaces then, but without Chromium's "
     "protection.", "Terminal-Tabs funktionieren dann ohne freie User-Namespaces, aber ohne den Schutz von Chromium."),
    ("notify", "A desktop notification for each new question of a sandbox.",
     "Eine Desktop-Benachrichtigung für jede neue Frage einer Sandbox."),
    ("auto_tabs", "Each session or sandbox that starts gets a terminal tab of its own.",
     "Jede Sitzung oder Sandbox, die startet, bekommt einen eigenen Terminal-Tab."),
    ("notify_seconds", "How long a notification stays on the screen.", "Wie lange eine Benachrichtigung zu sehen ist."),
    ("notify_style", "In the app's colours at a corner of the screen, or as the desktop's own notification.",
     "In den Farben der App an einer Ecke des Bildschirms oder als eigene Benachrichtigung des Desktops."),
    ("debug", "Logs every step with its time and result, to find a problem.",
     "Protokolliert jeden Schritt mit Zeit und Ergebnis, um einen Fehler zu finden."),
    ("approvals_interval", "How often the app looks for new questions; a question without an answer is refused "
     "after a while.", "Wie oft die App nach neuen Fragen sieht; eine Frage ohne Antwort wird nach einer Weile "
     "abgelehnt."),
    ("sandboxes_interval", "How often the list of running sandboxes is read again.",
     "Wie oft die Liste der laufenden Sandboxen neu gelesen wird."),
    ("connections_interval", "How often the connection log is read again.",
     "Wie oft das Verbindungsprotokoll neu gelesen wird."),
    ("sandbox_ai_home", "A sandbox-ai checkout to use instead of the sandbox-ai command; it applies at the next "
     "start.", "Ein sandbox-ai-Checkout statt des Befehls sandbox-ai; er gilt ab dem nächsten Start."),
    ("default_agent", "The agent a new board starts with.", "Der Agent, mit dem eine neue Tafel beginnt."),
    ("default_backend", "What isolates a new project: Podman (containers) or Bubblewrap.",
     "Was ein neues Projekt abschottet: Podman (Container) oder Bubblewrap."),
    ("default_template", "The preset a new board starts from.", "Die Vorlage, mit der eine neue Tafel beginnt."),
    ("board_grid", "Moved parts snap to a grid of this many pixels; 0 switches it off.",
     "Verschobene Teile rasten in einem Raster dieser Pixelzahl ein; 0 schaltet es ab."),
    ("telemetry", "Whether the agents send usage statistics, error reports and update checks; off is "
     "recommended.", "Ob die Agenten Nutzungsstatistiken, Fehlerberichte und Update-Abfragen senden; aus wird "
     "empfohlen."),
)


def term(label, language=None):
    """The short explanation (a Text) of a column title or form LABEL shown in the app's language (or LANGUAGE),
    or None. LABEL may end in a colon; & (a shortcut's mark) is ignored."""
    words = " ".join(str(label).replace("&&", "\0").replace("&", "").replace("\0", "&").split()).rstrip(":").strip()
    if not words:
        return None
    language = language or i18n.language()
    found = _TERMS_SHOWN.get(language)
    if found is None:
        found = _TERMS_SHOWN[language] = {
            ((i18n.TEXTS.get(key) or {}).get(language) or key): text for key, text in TERMS.items()}
    return found.get(words) or TERMS.get(words)


_TERMS_SHOWN = {}  # language: {a term as shown in it: its Text}
