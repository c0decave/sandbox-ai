"""Tours of single areas: for each page, a few steps that show how to use it, each pointing at the control it
is about (an attribute of the page: the window outlines it). Start one from Help > Area tours, with F1 on the page,
from the navigation's context menu or from the guided tour's step of that page. No Qt.

The texts are kept here in both languages (say() picks the app's language); tests check that every step has
both, that every control a step points at exists on its page, and that every page has a tour.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import i18n


@dataclass(frozen=True)
class Text:
    en: str
    de: str


@dataclass(frozen=True)
class AreaStep:
    target: str | None   # the page's attribute this step is about (None: the page as a whole)
    title: Text
    text: Text
    try_: Text


def say(text):
    """TEXT in the app's language."""
    return text.de if i18n.language() == "de" else text.en


def step(target, title, text, try_):
    return AreaStep(target, Text(*title), Text(*text), Text(*try_))


AREA_TOURS = {
    "launch": (
        step(None, ("Start a sandbox", "Eine Sandbox starten"),
             ("This page starts one agent in one sandbox: choose what it runs, what it may reach, and see the exact "
              "command before anything starts.",
              "Diese Seite startet einen Agenten in einer Sandbox: Sie wählen, was er ausführt und was er erreichen "
              "darf, und sehen den genauen Befehl, bevor etwas startet."),
             ("Open a project folder first (File > Open project folder).",
              "Öffnen Sie zuerst einen Projektordner (Datei > Projektordner öffnen).")),
        step("agent", ("The agent", "Der Agent"),
             ("Claude Code, Codex or OpenCode. Each has its own image; the System page says whether it is built.",
              "Claude Code, Codex oder OpenCode. Jeder hat sein eigenes Image; die Seite System sagt, ob es gebaut ist."),
             ("Pick the agent you are logged in to.", "Wählen Sie den Agenten, bei dem Sie angemeldet sind.")),
        step("host_choice", ("Where it starts", "Wo es startet"),
             ("This machine or a host of sandbox-ai hosts (over SSH). The Model choice, the Preset list beside "
              "Gallery… and the form's choosers (VPN profiles, kits, identities, MCP servers, image variants) are "
              "that host's; what its sandbox-ai does not know is greyed out.",
              "Dieser Rechner oder ein Host von sandbox-ai hosts (über SSH). Die Modellwahl, die Liste Vorlage neben "
              "Galerie… und die Auswahllisten des Formulars (VPN-Profile, Kits, Identitäten, MCP-Server, "
              "Image-Varianten) sind die dieses Hosts; was sein sandbox-ai nicht kennt, ist ausgegraut."),
             ("Choose a host and watch the command line begin with hosts run.",
              "Wählen Sie einen Host und sehen Sie, wie die Befehlszeile mit hosts run beginnt.")),
        step("model_choice", ("The model", "Das Modell"),
             ("The agent's own provider, or a model server registered on the chosen host (NAME or NAME:MODEL): "
              "--llm, without --model. Only this server lets the agent reach nothing but that server.",
              "Der eigene Anbieter des Agenten oder ein auf dem gewählten Host registrierter Modellserver (NAME "
              "oder NAME:MODELL): --llm, ohne --model. Nur dieser Server lässt den Agenten nichts als diesen Server "
              "erreichen."),
             ("Choose a server of yours, then the agent's provider again.",
              "Wählen Sie einen Ihrer Server, dann wieder den Anbieter des Agenten.")),
        step("profile", ("Your saved settings", "Ihre gespeicherten Einstellungen"),
             ("A profile here is a set of this form's settings you saved (Save as profile…): agent, options and "
              "agent arguments. Load brings them back. Network profiles such as development are in the form's "
              "Network group.",
              "Ein Profil ist hier ein Satz Einstellungen dieses Formulars, den Sie gespeichert haben (Als Profil "
              "speichern…): Agent, Optionen und Agent-Argumente. Laden holt sie zurück. Netzwerkprofile wie "
              "development stehen in der Gruppe Netzwerk des Formulars."),
             ("Save your settings once with Save as profile…, then Load them for the next start.",
              "Speichern Sie Ihre Einstellungen einmal mit Als Profil speichern… und laden Sie sie beim nächsten "
              "Start mit Laden.")),
        step("form", ("Every other option", "Alle weiteren Optionen"),
             ("The launcher's options in groups: network, proxy and privacy, Git, logins, kits, display, hardware. "
              "Each field says what it does; the ones that widen the sandbox are marked.",
              "Die Optionen des Launchers in Gruppen: Netzwerk, Proxy und Datenschutz, Git, Logins, Kits, Anzeige, "
              "Hardware. Jedes Feld sagt, was es tut; die, die die Sandbox erweitern, sind markiert."),
             ("Open the Network group and look at --allow-host.", "Öffnen Sie die Gruppe Netzwerk und sehen Sie sich "
              "--allow-host an.")),
        step("preview", ("The command", "Der Befehl"),
             ("What you chose, as the command line the app will run: copy it into a terminal to start the same "
              "sandbox there.",
              "Ihre Auswahl als die Befehlszeile, die die App ausführt: Kopieren Sie sie in ein Terminal, um dort "
              "dieselbe Sandbox zu starten."),
             ("Change an option and watch the command follow.", "Ändern Sie eine Option und sehen Sie, wie der Befehl "
              "folgt.")),
        step("dry_button", ("Check before starting", "Vor dem Start prüfen"),
             ("A dry run: the launcher checks everything (options, policy, images, network) and shows its plan, "
              "without starting anything.",
              "Ein Probelauf: Der Launcher prüft alles (Optionen, Richtlinie, Images, Netzwerk) und zeigt seinen "
              "Plan, ohne etwas zu starten."),
             ("Press it once before your first start.", "Drücken Sie ihn einmal vor Ihrem ersten Start.")),
    ),
    "board": (
        step(None, ("The board", "Die Tafel"),
             ("One sandbox as parts you connect: the agent, its folders, the network, MCP servers, rules. It writes "
              "the project's .sandbox-ai.json.",
              "Eine Sandbox als Teile, die Sie verbinden: der Agent, seine Ordner, das Netzwerk, MCP-Server, Regeln. "
              "Es schreibt die .sandbox-ai.json des Projekts."),
             ("Open a project, or start from a preset of the gallery.", "Öffnen Sie ein Projekt oder beginnen Sie mit "
              "einer Vorlage aus der Galerie.")),
        step("palette", ("Parts", "Teile"),
             ("Everything you can add. Double-click a part: the guide asks the few things it needs and explains "
              "each.",
              "Alles, was Sie hinzufügen können. Doppelklicken Sie ein Teil: Der Guide fragt das Wenige, das es "
              "braucht, und erklärt jedes."),
             ("Add a folder (a mount) read-only.", "Fügen Sie einen Ordner (ein Mount) nur lesbar hinzu.")),
        step("findings", ("What is wrong", "Was nicht stimmt"),
             ("The board checks itself after every change: a missing value, a part that is not connected, an "
              "option the policy refuses. Click a finding to see the part.",
              "Die Tafel prüft sich nach jeder Änderung: ein fehlender Wert, ein nicht verbundenes Teil, eine "
              "Option, die die Richtlinie ablehnt. Klicken Sie einen Befund an, um das Teil zu sehen."),
             ("Make the list empty before you apply.", "Leeren Sie die Liste, bevor Sie übernehmen.")),
        step("dry_button", ("Ask the launcher", "Den Launcher fragen"),
             ("A dry run of what the board says, by the launcher itself: its verdict is the one a start would give.",
              "Ein Probelauf dessen, was die Tafel sagt, durch den Launcher selbst: Sein Urteil ist das eines Starts."),
             ("Run it after each larger change.", "Starten Sie ihn nach jeder größeren Änderung.")),
        step("apply_button", ("Apply", "Übernehmen"),
             ("Shows the .sandbox-ai.json it will write, then writes and trusts it: the next start of the project "
              "uses it.",
              "Zeigt die .sandbox-ai.json, die es schreiben wird, schreibt und vertraut ihr dann: Der nächste Start "
              "des Projekts nutzt sie."),
             ("Apply, then start from the Start page.", "Übernehmen Sie und starten Sie dann von der Seite Start.")),
    ),
    "sandboxes": (
        step("table", ("What runs and what is kept", "Was läuft und was bleibt"),
             ("A tree: each host, under it its one-off runs, sessions that keep running when you close the app and "
              "MCP servers, with their state and project, and under each start the MCP servers, helpers and pod it "
              "has. A host that did not answer has its rows greyed (last seen N s ago); a start with an open "
              "question is marked asking.",
              "Ein Baum: jeder Host, darunter seine einmaligen Läufe, Sitzungen, die weiterlaufen, wenn Sie die App "
              "schließen, und MCP-Server mit Zustand und Projekt, und unter jedem Start seine MCP-Server, Helfer und "
              "sein Pod. Die Zeilen eines Hosts, der nicht antwortete, sind ausgegraut (zuletzt gesehen vor N s); "
              "ein Start mit offener Frage ist als fragt markiert."),
             ("Select a row to see its options and history.", "Wählen Sie eine Zeile, um Optionen und Verlauf zu "
              "sehen.")),
        step("only_project", ("Only this project", "Nur dieses Projekt"),
             ("Limits the list to the sandboxes of the project that is open.",
              "Beschränkt die Liste auf die Sandboxen des geöffneten Projekts."),
             ("Switch it off to see everything on this computer.", "Schalten Sie es aus, um alles auf diesem Rechner "
              "zu sehen.")),
        step("show_ended", ("What ended", "Was beendet ist"),
             ("Ended also lists the starts that ended. History… shows what happened to one, Clean up… removes what "
              "is left over after showing it first.",
              "Beendete listet auch die beendeten Starts. Verlauf… zeigt, was mit einem geschah, Aufräumen… entfernt "
              "Übriggebliebenes, nachdem es dies zuerst gezeigt hat."),
             ("Switch it on and look at the history of an old start.", "Schalten Sie es ein und sehen Sie sich den "
              "Verlauf eines alten Starts an.")),
        step("host_filter", ("Whose starts", "Wessen Starts"),
             ("Once there are hosts, Host lists all starts, this machine's or one host's. Each host is read on its "
              "own; one that does not answer keeps its rows greyed.",
              "Sobald es Hosts gibt, listet Host alle Starts, die dieses Rechners oder die eines Hosts. Jeder Host "
              "wird für sich gelesen; einer, der nicht antwortet, behält seine Zeilen ausgegraut."),
             ("Choose one host to see only what runs there.", "Wählen Sie einen Host, um nur zu sehen, was dort "
              "läuft.")),
        step("rename_button", ("Rename, history, clean up", "Umbenennen, Verlauf, Aufräumen"),
             ("Rename… gives the selected start another name; its run id, its row and its tab stay. History… shows "
              "what happened to it; Clean up… removes what is left over after showing it, and only that.",
              "Umbenennen… gibt dem gewählten Start einen anderen Namen; Lauf-ID, Zeile und Tab bleiben. Verlauf… "
              "zeigt, was mit ihm geschah; Aufräumen… entfernt Übriggebliebenes, nachdem es dies gezeigt hat, und "
              "nur das."),
             ("Look at the history of a start.", "Sehen Sie sich den Verlauf eines Starts an.")),
        step("devices_label", ("While it runs", "Während sie läuft"),
             ("For a running start: allow or revoke destinations, devices and limits until it ends; Add workspace… "
              "mounts a folder or a single file of its host (with a name of its own); Add MCP server… gives it a "
              "server or a helper agent now, without a restart, and keeps it for its later starts, offering the "
              "agent's restart where it needs one; Remove MCP server… takes one away.",
              "Für einen laufenden Start: Ziele, Geräte und Grenzen erlauben oder zurücknehmen, bis er endet; "
              "Arbeitsordner hinzufügen… bindet einen Ordner oder eine einzelne Datei seines Hosts ein (mit eigenem "
              "Namen); MCP-Server hinzufügen… gibt ihm sofort und ohne Neustart einen Server oder einen "
              "Helfer-Agenten, behält ihn für die späteren Starts und bietet den Neustart des Agenten an, wo er "
              "einen braucht; MCP-Server entfernen… nimmt einen weg."),
             ("Select a running session and add a read-only folder.",
              "Wählen Sie eine laufende Sitzung und fügen Sie einen nur lesbaren Ordner hinzu.")),
        step("resume_with_button", ("Resume with", "Fortsetzen mit"),
             ("Starts a kept start again with workspaces, mounts or MCP servers added (kept for its later starts), "
              "or restarts a running one now; the agent continues its conversation.",
              "Startet einen aufbewahrten Start erneut mit hinzugefügten Arbeitsordnern, Mounts oder MCP-Servern (für "
              "seine späteren Starts behalten) oder startet einen laufenden sofort neu; der Agent setzt sein "
              "Gespräch fort."),
             ("Resume a stopped session with one more workspace.",
              "Setzen Sie eine gestoppte Sitzung mit einem weiteren Arbeitsordner fort.")),
        step("stop_button", ("Stop", "Stoppen"),
             ("Ends the selected sandbox; its launcher cleans up. A session can also be paused and resumed.",
              "Stoppt die gewählte Sandbox; ihr Launcher räumt auf. Eine Sitzung lässt sich auch pausieren und "
              "fortsetzen."),
             ("Stop a run you no longer need.", "Stoppen Sie einen Lauf, den Sie nicht mehr brauchen.")),
    ),
    "approvals": (
        step("open", ("Open questions", "Offene Fragen"),
             ("A sandbox waits here when its agent wants something not allowed yet: a destination, a push with "
              "warnings, an MCP tool, a page to open. Without an answer within a minute it is refused; a page to "
              "open waits until it is answered.",
              "Eine Sandbox wartet hier, wenn ihr Agent etwas noch nicht Erlaubtes will: ein Ziel, einen Push mit "
              "Warnungen, ein MCP-Werkzeug, eine zu öffnende Seite. Ohne Antwort innerhalb einer Minute wird es "
              "abgelehnt; eine zu öffnende Seite wartet, bis sie beantwortet ist."),
             ("Keep this page open while an agent works, or let the tray tell you.",
              "Lassen Sie die Seite offen, während ein Agent arbeitet, oder lassen Sie sich vom Tray benachrichtigen.")),
        step("host_filter", ("Whose questions", "Wessen Fragen"),
             ("The questions of this machine and of every host are listed together; here you narrow them to this "
              "machine or one host.",
              "Die Fragen dieses Rechners und jedes Hosts stehen zusammen; hier beschränken Sie sie auf diesen "
              "Rechner oder einen Host."),
             ("Choose This machine.", "Wählen Sie Dieser Rechner.")),
        step("scope", ("For how long", "Für wie lange"),
             ("How long an allow lasts: once, for the run, or always (kept for the project). The security mode "
              "decides what is offered.",
              "Wie lange ein Erlauben gilt: einmal, für den Lauf oder immer (für das Projekt gemerkt). Der "
              "Sicherheitsmodus bestimmt, was angeboten wird."),
             ("Choose once while you are unsure.", "Wählen Sie einmal, solange Sie unsicher sind.")),
        step("allow_button", ("Allow, deny, block", "Erlauben, ablehnen, sperren"),
             ("Allow (Ctrl+Return) lets it through; for another host's page it reads Open here and allow and opens "
              "the page in your browser here. Deny for this run (Ctrl+Shift+D) refuses it until the run ends, Deny "
              "once only this time, Block always… (Ctrl+Shift+K) at every later start too, after asking.",
              "Erlauben (Strg+Eingabe) lässt es durch; für eine Seite eines anderen Hosts heißt es Hier öffnen und "
              "erlauben und öffnet die Seite hier in Ihrem Browser. Für diesen Lauf ablehnen (Strg+Umschalt+D) weist "
              "es bis zum Ende des Laufs ab, Einmal ablehnen nur dieses Mal, Immer sperren… (Strg+Umschalt+K) nach "
              "einer Rückfrage auch bei jedem späteren Start."),
             ("Answer the next question here.", "Beantworten Sie die nächste Frage hier.")),
        step("always", ("What you allowed always", "Was Sie immer erlaubt haben"),
             ("Your standing answers, of this machine or of a host you choose below. Forget one and it is asked "
              "again.",
              "Ihre dauerhaften Antworten, die dieses Rechners oder eines Hosts, den Sie darunter wählen. Vergessen "
              "Sie eine, wird wieder gefragt."),
             ("Look through them now and then.", "Sehen Sie sie ab und zu durch.")),
    ),
    "terminals": (
        step("tabs", ("Terminals in the app", "Terminals in der App"),
             ("The sandboxes started in the app, builds and logins, each in a tab. The agent runs in tmux window 0, a "
              "shell in window 1.",
              "Die in der App gestarteten Sandboxen, Builds und Logins, jede in einem Tab. Der Agent läuft in "
              "tmux-Fenster 0, eine Shell in Fenster 1."),
             ("Ctrl-b d detaches from a session: it keeps running.", "Strg-b d löst sich von einer Sitzung: Sie "
              "läuft weiter.")),
        step("starts", ("Sessions", "Sitzungen"),
             ("Below the terminals, folded until you open it: what runs on this machine (or a host), however it was "
              "started: the command line, the app, the web interface. A start in tmux opens with one click; the "
              "others say why there is nothing to attach.",
              "Unter den Terminals, zugeklappt, bis Sie es öffnen: was auf diesem Rechner (oder einem Host) läuft, "
              "gleich wie es gestartet wurde: Kommandozeile, App, Weboberfläche. Ein Start in tmux öffnet sich mit "
              "einem Klick; die anderen sagen, warum es nichts zum Verbinden gibt."),
             ("Open the terminal of a start you began in a terminal.",
              "Öffnen Sie das Terminal eines Starts, den Sie in einem Terminal begonnen haben.")),
        step("fetch", ("No terminal tabs?", "Keine Terminal-Tabs?"),
             ("The tabs need xterm.js, fetched once and checked against its pinned digest; without it commands open "
              "in a terminal window.",
              "Die Tabs brauchen xterm.js, einmal geladen und gegen seinen festgelegten Digest geprüft; ohne es "
              "öffnen Befehle in einem Terminalfenster."),
             ("Fetch it if the page offers it.", "Laden Sie es, wenn die Seite es anbietet.")),
    ),
    "connections": (
        step("directory", ("Which logs", "Welche Protokolle"),
             ("Every connection the proxy saw, from the log directory of a project or of all runs.",
              "Jede Verbindung, die der Proxy sah, aus dem Protokollverzeichnis eines Projekts oder aller Läufe."),
             ("Pick the project you work on.", "Wählen Sie das Projekt, an dem Sie arbeiten.")),
        step("filter", ("Find a host", "Einen Host finden"),
             ("Filter by host, path or method; refused shows only what was not let through.",
              "Filtern Sie nach Host, Pfad oder Methode; abgewiesen zeigt nur, was nicht durchgelassen wurde."),
             ("Type a host your agent used.", "Geben Sie einen Host ein, den Ihr Agent nutzte.")),
        step("refused", ("What was refused", "Was abgewiesen wurde"),
             ("Each refusal comes with the option that would allow it.",
              "Jede Abweisung kommt mit der Option, die sie erlauben würde."),
             ("Switch it on after a failed download.", "Schalten Sie es nach einem fehlgeschlagenen Download ein.")),
        step("detail", ("One request", "Eine Anfrage"),
             ("Method, URL, headers and bodies (credentials redacted), the rule that decided and the answer.",
              "Methode, URL, Header und Bodies (Anmeldedaten geschwärzt), die Regel, die entschied, und die Antwort."),
             ("Select a row to see it.", "Wählen Sie eine Zeile, um sie zu sehen.")),
    ),
    "activity": (
        step("source", ("Which run", "Welcher Lauf"),
             ("What happened in a run, in order: prompts, replies, tool calls, commands, files, connections, "
              "questions, the taint.",
              "Was in einem Lauf geschah, der Reihe nach: Prompts, Antworten, Werkzeugaufrufe, Befehle, Dateien, "
              "Verbindungen, Fragen, der Taint."),
             ("Pick the run you want to follow.", "Wählen Sie den Lauf, dem Sie folgen wollen.")),
        step("kinds_button", ("Only some kinds", "Nur manche Arten"),
             ("Show only prompts, or only commands and file changes.",
              "Zeigen Sie nur Prompts oder nur Befehle und Dateiänderungen."),
             ("Leave only commands checked.", "Lassen Sie nur Befehle angehakt.")),
        step("follow", ("Live", "Live"),
             ("New events appear as they happen, nothing is lost between the list and the live view.",
              "Neue Ereignisse erscheinen, wenn sie geschehen; zwischen Liste und Live-Ansicht geht nichts verloren."),
             ("Switch it on while an agent works.", "Schalten Sie es ein, während ein Agent arbeitet.")),
        step("detail", ("One event", "Ein Ereignis"),
             ("Everything about the selected event; from there to its connection or file.",
              "Alles zum gewählten Ereignis; von dort zu seiner Verbindung oder Datei."),
             ("Select a tool call.", "Wählen Sie einen Werkzeugaufruf.")),
    ),
    "statistics": (
        step("directory", ("Tokens and calls", "Tokens und Aufrufe"),
             ("Tokens, requests and tool calls per run and per agent, from a project's logs.",
              "Tokens, Anfragen und Werkzeugaufrufe pro Lauf und pro Agent, aus den Protokollen eines Projekts."),
             ("Pick a project and compare two runs.", "Wählen Sie ein Projekt und vergleichen Sie zwei Läufe.")),
    ),
    "files": (
        step("roots", ("Whose files", "Wessen Dateien"),
             ("The HOME and workspaces of a sandbox: look into them without opening a shell in it.",
              "HOME und Arbeitsordner einer Sandbox: hineinsehen, ohne eine Shell darin zu öffnen."),
             ("Pick a running sandbox.", "Wählen Sie eine laufende Sandbox.")),
        step("table", ("Two panes: copy in and out", "Zwei Bereiche: hinein- und herauskopieren"),
             ("The sandbox on the left, this machine on the right. Copy (F5) or Move (F6) takes what is selected in "
              "one pane into the other's folder, without giving the sandbox a mount; Tab switches panes. Links "
              "inside are never followed.",
              "Links die Sandbox, rechts dieser Rechner. Kopieren (F5) oder Verschieben (F6) bringt das in einem "
              "Bereich Gewählte in den Ordner des anderen, ohne der Sandbox ein Mount zu geben; Tab wechselt den "
              "Bereich. Links darin werden nie verfolgt."),
             ("Copy a small file in.", "Kopieren Sie eine kleine Datei hinein.")),
        step("replace", ("Replace", "Ersetzen"),
             ("Allows overwriting a file of the same name; off, nothing is ever replaced.",
              "Erlaubt das Überschreiben einer gleichnamigen Datei; aus, wird nie etwas ersetzt."),
             ("Leave it off unless you mean it.", "Lassen Sie es aus, außer Sie meinen es so.")),
    ),
    "kits": (
        step("table", ("Kits", "Kits"),
             ("Skills, agents, commands, prompts, memory templates, hooks and plugins, in kits a project uses with "
              "--kit.",
              "Skills, Agenten, Befehle, Prompts, Memory-Vorlagen, Hooks und Plugins, in Kits, die ein Projekt mit "
              "--kit nutzt."),
             ("Select a kit to see what it holds.", "Wählen Sie ein Kit, um zu sehen, was es enthält.")),
        step("new_skill_button", ("Your own skill", "Ein eigener Skill"),
             ("A new skill in a kit of yours: a SKILL.md the agent reads when it fits the task.",
              "Ein neuer Skill in einem Ihrer Kits: eine SKILL.md, die der Agent liest, wenn sie zur Aufgabe passt."),
             ("Create one with a short description.", "Legen Sie einen mit kurzer Beschreibung an.")),
        step("use_button", ("Use it", "Verwenden"),
             ("Adds the kit to the current project's configuration.",
              "Fügt das Kit der Konfiguration des aktuellen Projekts hinzu."),
             ("Use your kit in the open project.", "Verwenden Sie Ihr Kit im offenen Projekt.")),
        step("trust_button", ("Hooks need your approval", "Hooks brauchen Ihre Freigabe"),
             ("A hook runs code at the agent's events; none runs before you approved its exact text.",
              "Ein Hook führt Code bei Ereignissen des Agenten aus; keiner läuft, bevor Sie seinen genauen Text "
              "freigegeben haben."),
             ("Read a kit's hooks before approving them.", "Lesen Sie die Hooks eines Kits, bevor Sie sie "
              "freigeben.")),
        step("market", ("Marketplaces", "Marktplätze"),
             ("Every plugin marketplace of the host with its plugins and their state: add a plugin to the kit chosen "
              "(the default kit or one of yours), turn it on or off, or take it out; the agents' own configuration "
              "is only read.",
              "Jeder Plugin-Marktplatz des Hosts mit seinen Plugins und deren Zustand: ein Plugin zum gewählten Kit "
              "(dem Standard-Kit oder einem Ihrer Kits) hinzufügen, ein- oder ausschalten oder herausnehmen; die "
              "eigene Konfiguration der Agenten wird nur gelesen."),
             ("Pick a marketplace and look at its plugins.", "Wählen Sie einen Marktplatz und sehen Sie seine "
              "Plugins an.")),
    ),
    "mcp": (
        step("servers", ("Your MCP servers", "Ihre MCP-Server"),
             ("Each runs in a sandbox or container of its own, with only the network and folders its definition "
              "names; agents reach it through a relay that applies your rules.",
              "Jeder läuft in einer eigenen Sandbox oder einem eigenen Container, nur mit dem Netzwerk und den "
              "Ordnern, die seine Definition nennt; Agenten erreichen ihn über ein Relay, das Ihre Regeln anwendet."),
             ("Select one to see its definition.", "Wählen Sie einen, um seine Definition zu sehen.")),
        step("catalog", ("The catalogs", "Die Kataloge"),
             ("Audited servers pinned by digest (search, web pages as text, analysis tools). Adding one defines it; "
              "a start names it with --mcp.",
              "Geprüfte Server, per Digest festgelegt (Suche, Webseiten als Text, Analysewerkzeuge). Hinzufügen "
              "definiert ihn; ein Start nennt ihn mit --mcp."),
             ("Add a search server and connect it on the board.", "Fügen Sie einen Suchserver hinzu und verbinden "
              "Sie ihn auf der Tafel.")),
    ),
    "llm": (
        step("host_box", ("Whose servers", "Wessen Server"),
             ("This machine or a host of sandbox-ai hosts: the list and every button act there.",
              "Dieser Rechner oder ein Host von sandbox-ai hosts: die Liste und jeder Knopf wirken dort."),
             ("Keep this machine for now.", "Bleiben Sie vorerst bei diesem Rechner.")),
        step("servers", ("Your LLM servers", "Ihre LLM-Server"),
             ("Model servers of your own, kept by name: Ollama, LM Studio, llama.cpp, vLLM or another "
              "OpenAI-compatible one, local or behind a VPN. A start uses one with --llm NAME and reaches only its "
              "address.",
              "Eigene Modellserver, unter einem Namen aufbewahrt: Ollama, LM Studio, llama.cpp, vLLM oder ein "
              "anderer OpenAI-kompatibler, lokal oder hinter einem VPN. Ein Start nutzt einen mit --llm NAME und "
              "erreicht nur seine Adresse."),
             ("Select a server to act on it.", "Wählen Sie einen Server, um mit ihm zu arbeiten.")),
        step("add_button", ("Add one", "Einen hinzufügen"),
             ("Its name, kind and URL, optionally a model, a VPN profile and a key; the key is kept as a secret of "
              "sandbox-ai, and the agent only ever sees a placeholder.",
              "Name, Art und URL, optional ein Modell, ein VPN-Profil und ein Schlüssel; der Schlüssel wird als "
              "Geheimnis von sandbox-ai aufbewahrt, und der Agent sieht immer nur einen Platzhalter."),
             ("Add the Ollama on this machine: http://127.0.0.1:11434.", "Fügen Sie das Ollama auf diesem Rechner "
              "hinzu: http://127.0.0.1:11434.")),
        step("check_button", ("Check it", "Prüfen"),
             ("Reachability, TLS, the key, the model and the API the chosen agent needs, before a start depends "
              "on it.",
              "Erreichbarkeit, TLS, der Schlüssel, das Modell und die API, die der gewählte Agent braucht, bevor ein "
              "Start davon abhängt."),
             ("Check the server for the agent you use.", "Prüfen Sie den Server für den Agenten, den Sie nutzen.")),
        step("models_button", ("Its models", "Seine Modelle"),
             ("What the server offers now; a start picks one with --llm NAME:MODEL.",
              "Was der Server gerade anbietet; ein Start wählt eines mit --llm NAME:MODELL."),
             ("List the models and note one for your next start.", "Listen Sie die Modelle auf und merken Sie sich "
              "eines für Ihren nächsten Start.")),
    ),
    "images": (
        step("images", ("The images", "Die Images"),
             ("Each agent's image and your variants: built or not, when, how big, and whether their extension "
              "changed since.",
              "Das Image jedes Agenten und Ihre Varianten: gebaut oder nicht, wann, wie groß und ob sich ihre "
              "Erweiterung seitdem geändert hat."),
             ("Select the image of your agent.", "Wählen Sie das Image Ihres Agenten.")),
        step("build_button", ("Build", "Bauen"),
             ("Builds the base image, then the extension layer; once per agent, again after an update.",
              "Baut das Basis-Image, dann die Erweiterungsschicht; einmal pro Agent, wieder nach einem Update."),
             ("Build the image you use.", "Bauen Sie das Image, das Sie nutzen.")),
        step("new_button", ("A variant", "Eine Variante"),
             ("A copy of an agent's image with its own extension (more tools, a media recipe); start it with "
              "--image-variant.",
              "Eine Kopie des Images eines Agenten mit eigener Erweiterung (mehr Werkzeuge, ein Media-Rezept); "
              "starten Sie sie mit --image-variant."),
             ("Make a variant for a project that needs more.", "Legen Sie eine Variante für ein Projekt an, das mehr "
              "braucht.")),
        step("editor_tabs", ("The extension", "Die Erweiterung"),
             ("Debian packages, npm packages, Python tools, files and a Containerfile fragment, checked as you type; "
              "the final Containerfile is shown too.",
              "Debian-Pakete, npm-Pakete, Python-Werkzeuge, Dateien und ein Containerfile-Fragment, beim Tippen "
              "geprüft; das fertige Containerfile wird auch gezeigt."),
             ("Add one Debian package.", "Fügen Sie ein Debian-Paket hinzu.")),
        step("save_button", ("Save, then build", "Speichern, dann bauen"),
             ("Saving changes the extension; the image changes only when you build it again.",
              "Speichern ändert die Erweiterung; das Image ändert sich erst, wenn Sie es neu bauen."),
             ("Save, then build.", "Speichern Sie, dann bauen Sie.")),
    ),
    "credentials": (
        step("logins", ("Agent logins", "Logins der Agenten"),
             ("Each agent's login, kept on this computer; a sandbox gets it when it starts, never more.",
              "Der Login jedes Agenten, auf diesem Rechner aufbewahrt; eine Sandbox bekommt ihn beim Start, nie "
              "mehr."),
             ("Log in to your agent once.", "Melden Sie sich einmal bei Ihrem Agenten an.")),
        step("secrets", ("Secrets", "Geheimnisse"),
             ("API keys and tokens in your keyring (or private files). A start names them; with --api-key or "
              "--publish they never enter the sandbox at all.",
              "API-Schlüssel und Tokens in Ihrem Schlüsselbund (oder privaten Dateien). Ein Start nennt sie; mit "
              "--api-key oder --publish kommen sie nie in die Sandbox."),
             ("Keep one secret by name.", "Bewahren Sie ein Geheimnis unter einem Namen auf.")),
        step("vpn", ("VPN profiles", "VPN-Profile"),
             ("WireGuard, OpenVPN, Mullvad or Tailscale in front of a sandbox; keys and configs stay here.",
              "WireGuard, OpenVPN, Mullvad oder Tailscale vor einer Sandbox; Schlüssel und Konfigurationen bleiben "
              "hier."),
             ("Add a profile from a WireGuard file.", "Legen Sie ein Profil aus einer WireGuard-Datei an.")),
    ),
    "team": (
        step("hosts", ("Whose teams", "Wessen Teams"),
             ("This machine or a host of sandbox-ai hosts: the list and every button act there.",
              "Dieser Rechner oder ein Host von sandbox-ai hosts: die Liste und jeder Knopf wirken dort."),
             ("Keep this machine for now.", "Bleiben Sie vorerst bei diesem Rechner.")),
        step("table", ("The teams", "Die Teams"),
             ("Each team with its members and its workspace; select one to see its board.",
              "Jedes Team mit seinen Mitgliedern und seinem Arbeitsordner; wählen Sie eines, um seine Tafel zu "
              "sehen."),
             ("Select a team.", "Wählen Sie ein Team.")),
        step("start_button", ("Start one", "Eines starten"),
             ("Two or three agents on one workspace, the first leads; each member starts in a sandbox of its own.",
              "Zwei oder drei Agenten auf einem Arbeitsordner, der erste leitet; jedes Mitglied startet in einer "
              "eigenen Sandbox."),
             ("Start Claude Code with Codex on the current project.", "Starten Sie Claude Code mit Codex auf dem "
              "aktuellen Projekt.")),
        step("board", ("The board", "Die Tafel"),
             ("The tasks between the members, and those you post: who for, their state, what they ask.",
              "Die Aufgaben zwischen den Mitgliedern und die, die Sie stellen: für wen, ihr Stand, was sie verlangen."),
             ("Post a task for a member and watch it change to done.", "Stellen Sie einem Mitglied eine Aufgabe und "
              "sehen Sie zu, wie sie erledigt wird.")),
    ),
    "system": (
        step("checks", ("What sandbox-ai needs", "Was sandbox-ai braucht"),
             ("Podman or Bubblewrap, the images, the network, the keyring: each check with what fixes it.",
              "Podman oder Bubblewrap, die Images, das Netzwerk, der Schlüsselbund: jede Prüfung mit dem, was sie "
              "behebt."),
             ("Run the checks and fix the first red one.", "Führen Sie die Prüfungen aus und beheben Sie die erste "
              "rote.")),
        step("theme", ("Look and language", "Aussehen und Sprache"),
             ("The app's theme and language; the theme also follows your desktop when set to system.",
              "Farbschema und Sprache der App; das Farbschema folgt Ihrem Desktop, wenn es auf System steht."),
             ("Try another theme.", "Probieren Sie ein anderes Farbschema.")),
        step("update_button", ("Update", "Aktualisieren"),
             ("Installs the newest sandbox-ai and the app; the old version stays until the new one works.",
              "Installiert das neueste sandbox-ai und die App; die alte Version bleibt, bis die neue funktioniert."),
             ("Look for an update.", "Suchen Sie nach einem Update.")),
    ),
    "hosts": (
        step("table", ("Other machines", "Andere Rechner"),
             ("Machines whose sandbox-ai this app manages over SSH, such as LXC containers: whether each answered, "
              "its sandbox-ai version, the starts running and the questions waiting there.",
              "Rechner, deren sandbox-ai diese App über SSH verwaltet, etwa LXC-Container: ob jeder antwortete, "
              "seine sandbox-ai-Version, die dort laufenden Starts und die wartenden Fragen."),
             ("Add a host with its SSH address.", "Fügen Sie einen Host mit seiner SSH-Adresse hinzu.")),
        step("test_button", ("Test it", "Testen"),
             ("SSH, the host's sandbox-ai and its doctor: whether everything a sandbox needs is there.",
              "SSH, das sandbox-ai des Hosts und sein doctor: ob alles da ist, was eine Sandbox braucht."),
             ("Test the host you added.", "Testen Sie den Host, den Sie hinzugefügt haben.")),
        step("starts_button", ("Its starts", "Seine Starts"),
             ("Show starts opens Sandboxes & sessions with only this host's starts: attach, pause and stop them "
              "there.",
              "Starts zeigen öffnet Sandboxen & Sitzungen nur mit den Starts dieses Hosts: Verbinden, pausieren und "
              "beenden Sie sie dort."),
             ("Show the starts of a host.", "Zeigen Sie die Starts eines Hosts.")),
        step("questions_button", ("Its questions", "Seine Fragen"),
             ("Show questions opens the Approvals page with only this host's questions.",
              "Fragen zeigen öffnet die Seite Freigaben nur mit den Fragen dieses Hosts."),
             ("Answer a host's question there.", "Beantworten Sie dort eine Frage des Hosts.")),
        step("tabs", ("Doctor, updates, defaults", "Doctor, Updates, Standards"),
             ("The chosen host's doctor, its updates and its defaults for every start there: security mode, "
              "canaries, how long kept starts stay, telemetry. A step down in security is asked first.",
              "Doctor, Updates und Standards des gewählten Hosts für jeden Start dort: Sicherheitsmodus, Canaries, "
              "wie lange aufbewahrte Starts bleiben, Telemetrie. Ein Schritt zu weniger Sicherheit wird vorher "
              "erfragt."),
             ("Open Defaults and look at the host's security mode.", "Öffnen Sie Standards und sehen Sie sich den "
              "Sicherheitsmodus des Hosts an.")),
    ),
    "help": (
        step("index", ("Every topic", "Jedes Thema"),
             ("Every page and feature of the app, and the ideas behind sandbox-ai: sandbox, network, proxy, "
              "approvals, presets, kits and more.",
              "Jede Seite und Funktion der App und die Ideen hinter sandbox-ai: Sandbox, Netzwerk, Proxy, "
              "Freigaben, Vorlagen, Kits und mehr."),
             ("Select Presets under Concepts.", "Wählen Sie Vorlagen unter Konzepte.")),
        step("search", ("Search", "Suchen"),
             ("Finds every section that contains all the words you type, in English and German.",
              "Findet jeden Abschnitt, der alle eingegebenen Wörter enthält, auf Englisch und Deutsch."),
             ("Type proxy.", "Geben Sie proxy ein.")),
        step("language", ("English or German", "Englisch oder Deutsch"),
             ("The help follows the app's language; switch it here without restarting.",
              "Die Hilfe folgt der Sprache der App; wechseln Sie sie hier ohne Neustart."),
             ("Switch to the other language and back.", "Wechseln Sie in die andere Sprache und zurück.")),
        step("tours", ("Tours", "Touren"),
             ("Walks through whole tasks across pages: the first start, your own construct saved as a preset, "
              "approvals, the preset gallery, and this help.",
              "Wege durch ganze Aufgaben über mehrere Seiten: der erste Start, ein eigenes Konstrukt als Vorlage, "
              "Freigaben, die Vorlagen-Galerie und diese Hilfe."),
             ("Start the tour of your own construct.", "Starten Sie die Tour zum eigenen Konstrukt.")),
    ),
}


def tour_of(page):
    """The steps of PAGE's tour (KeyError for a page without one)."""
    return AREA_TOURS[page]


# -- tours through whole tasks, across pages --
#
# A step names the page it happens on and the control it is about (an attribute of that page, outlined while the
# step is shown), and optionally a CHECK the window evaluates to tick the step off when it is done (guide_view
# CHECKS: "project", "page:KEY", "board-has:KIND", "board-connected:KIND", "board-option:KIND:OPTION",
# "board-no-errors", "board-applied", "preset-mine", "help-search", "tip-opened").

@dataclass(frozen=True)
class TaskStep:
    page: str
    target: str | None
    title: Text
    text: Text
    try_: Text
    check: str = ""


@dataclass(frozen=True)
class TaskTour:
    id: str
    title: Text
    summary: Text
    steps: tuple


def task(page, target, title, text, try_, check=""):
    return TaskStep(page, target, Text(*title), Text(*text), Text(*try_), check)


TASK_TOURS = {tour.id: tour for tour in (
    TaskTour("first-start", Text("First start: run an agent", "Erster Start: einen Agenten ausführen"),
             Text("From a fresh installation to an agent working in a sandbox: check the host, build the image, "
                  "log in, open a project, choose what the agent may reach, and start it.",
                  "Von einer frischen Installation zu einem Agenten, der in einer Sandbox arbeitet: Host prüfen, "
                  "Image bauen, anmelden, Projekt öffnen, wählen, was der Agent erreichen darf, und starten."), (
        task("system", "checks", ("Check this computer", "Diesen Rechner prüfen"),
             ("sandbox-ai needs rootless Podman or Bubblewrap and a few more things. The host check (sandbox-ai "
              "doctor) lists each with what fixes it.",
              "sandbox-ai braucht rootless Podman oder Bubblewrap und einiges mehr. Die Host-Prüfung (sandbox-ai "
              "doctor) nennt alles mit dem, was es behebt."),
             ("Run the check and fix the first red line.", "Führen Sie die Prüfung aus und beheben Sie die erste "
              "rote Zeile."), "page:system"),
        task("images", "build_button", ("Build the agent's image", "Das Image des Agenten bauen"),
             ("Each agent runs from its own image. Build it once; again after an update.",
              "Jeder Agent läuft aus seinem eigenen Image. Bauen Sie es einmal; wieder nach einem Update."),
             ("Select your agent's image and press Build.", "Wählen Sie das Image Ihres Agenten und drücken Sie "
              "Bauen."), "page:images"),
        task("credentials", "logins", ("Log in once", "Einmal anmelden"),
             ("The agent's login is kept here, outside every sandbox; each start gets a copy.",
              "Der Login des Agenten wird hier aufbewahrt, außerhalb jeder Sandbox; jeder Start bekommt eine "
              "Kopie."),
             ("Log in to your agent.", "Melden Sie sich bei Ihrem Agenten an."), "page:credentials"),
        task("launch", "open_button", ("Open the project", "Das Projekt öffnen"),
             ("The project folder becomes /work in the sandbox; nothing else of your home is visible.",
              "Der Projektordner wird in der Sandbox zu /work; sonst ist nichts von Ihrem Home sichtbar."),
             ("Open a project folder.", "Öffnen Sie einen Projektordner."), "project"),
        task("launch", "agent", ("Choose the agent", "Den Agenten wählen"),
             ("Claude Code, Codex or OpenCode: the form below shows its options.",
              "Claude Code, Codex oder OpenCode: Das Formular darunter zeigt seine Optionen."),
             ("Pick the agent you logged in to.", "Wählen Sie den Agenten, bei dem Sie angemeldet sind."),
             "page:launch"),
        task("launch", "form", ("What it may reach", "Was er erreichen darf"),
             ("Without more, only the agent's provider is reachable. A profile such as development opens package "
              "registries and GitHub; single hosts go into --allow-host.",
              "Ohne mehr ist nur der Anbieter des Agenten erreichbar. Ein Profil wie development öffnet "
              "Paketquellen und GitHub; einzelne Hosts kommen in --allow-host."),
             ("Choose the profile development in the Network group.", "Wählen Sie das Profil development in der "
              "Gruppe Netzwerk.")),
        task("launch", "dry_button", ("Check before starting", "Vor dem Start prüfen"),
             ("A dry run asks the launcher about exactly these options; nothing starts.",
              "Ein Probelauf fragt den Launcher nach genau diesen Optionen; nichts startet."),
             ("Press Dry run and read the plan.", "Drücken Sie Probelauf und lesen Sie den Plan.")),
        task("launch", "session", ("Start", "Starten"),
             ("Start opens a terminal tab (or a terminal window); as a session the sandbox keeps running when you "
              "close the app.",
              "Start öffnet einen Terminal-Tab (oder ein Terminalfenster); als Sitzung läuft die Sandbox weiter, wenn "
              "Sie die App schließen."),
             ("Press Start.", "Drücken Sie Start.")),
        task("terminals", "tabs", ("The agent works", "Der Agent arbeitet"),
             ("The agent runs in tmux window 0, a shell in window 1. Questions it asks appear on Approvals.",
              "Der Agent läuft in tmux-Fenster 0, eine Shell in Fenster 1. Seine Fragen erscheinen unter "
              "Freigaben."),
             ("Give the agent a first task.", "Geben Sie dem Agenten eine erste Aufgabe."), "page:terminals"),
    )),
    TaskTour("own-construct", Text("Your own construct, saved as a preset",
                                   "Ein eigenes Konstrukt, als Vorlage gespeichert"),
             Text("Build a sandbox on the board part by part: add and connect parts, set network, folders and "
                  "devices, check it, apply it, save it as your own preset and start from it again.",
                  "Bauen Sie eine Sandbox auf der Tafel Teil für Teil: Teile hinzufügen und verbinden, Netzwerk, "
                  "Ordner und Geräte einstellen, prüfen, übernehmen, als eigene Vorlage speichern und wieder davon "
                  "starten."), (
        task("board", None, ("The board", "Die Tafel"),
             ("The board starts with the agent, the connection proxy and the network: the parts every sandbox "
              "has. Open a project first: its folder becomes the first workspace.",
              "Die Tafel beginnt mit Agent, Verbindungs-Proxy und Netzwerk: den Teilen, die jede Sandbox hat. Öffnen "
              "Sie zuerst ein Projekt: Sein Ordner wird der erste Arbeitsordner."),
             ("Open a project folder (File > Open project folder).", "Öffnen Sie einen Projektordner (Datei > "
              "Projektordner öffnen)."), "project"),
        task("board", "palette", ("Add a folder", "Einen Ordner hinzufügen"),
             ("Drag Mount from the parts onto the board: it lands where you drop it, and the guide asks for the "
              "host folder, the path in the sandbox and whether it may be written.",
              "Ziehen Sie Einbindung aus den Teilen auf die Tafel: Sie landet, wo Sie sie ablegen, und der Guide "
              "fragt nach dem Ordner des Hosts, dem Pfad in der Sandbox und ob er beschreibbar sein darf."),
             ("Drag Mount onto the board (or select it and press Enter).",
              "Ziehen Sie Einbindung auf die Tafel (oder wählen Sie sie und drücken Sie Enter)."), "board-has:mount"),
        task("board", "view", ("Connect it", "Verbinden"),
             ("A folder counts only while it is connected to the agent. Drag from the mount's right port onto "
              "the agent; a dashed line follows and valid targets light up.",
              "Ein Ordner zählt nur, solange er mit dem Agenten verbunden ist. Ziehen Sie vom rechten Anschluss der "
              "Einbindung auf den Agenten; eine gestrichelte Linie folgt, und gültige Ziele werden umrandet."),
             ("Connect the mount to the agent.", "Verbinden Sie die Einbindung mit dem Agenten."),
             "board-connected:mount"),
        task("board", "side", ("Set it up", "Einrichten"),
             ("The inspector shows the selected part's settings. Each change is one step of Undo.",
              "Der Inspektor zeigt die Einstellungen des gewählten Teils. Jede Änderung ist ein Schritt von "
              "Rückgängig."),
             ("Select the mount and choose its host folder and mode.", "Wählen Sie die Einbindung und legen Sie "
                                                                       "Ordner und Modus fest.")),
        task("board", "view", ("Open a host", "Einen Host öffnen"),
             ("Select the Network part: --allow-host lists single hosts the agent may reach, one per line; a "
              "profile opens a set at once.",
              "Wählen Sie das Teil Netzwerk: --allow-host nennt einzelne Hosts, die der Agent erreichen darf, "
              "einen pro Zeile; ein Profil öffnet mehrere auf einmal."),
             ("Add one host to --allow-host.", "Tragen Sie einen Host in --allow-host ein."),
             "board-option:network:--allow-host"),
        task("board", "palette", ("A device or limits", "Ein Gerät oder Grenzen"),
             ("Devices and GPU hands host devices to the agent (it weakens the isolation); Limits caps memory, "
              "CPUs and processes.",
              "Geräte und GPU gibt dem Agenten Geräte des Hosts (das schwächt die Isolation); Grenzen begrenzt "
              "Speicher, CPUs und Prozesse."),
             ("Add Limits and set a memory limit.", "Fügen Sie Grenzen hinzu und setzen Sie eine "
              "Speichergrenze."), "board-has:limits"),
        task("board", "findings", ("Nothing wrong left", "Nichts Falsches mehr"),
             ("The board checks itself after every change. Errors (✘) stop Apply and Start; double-click one to "
              "select its part.",
              "Die Tafel prüft sich nach jeder Änderung. Fehler (✘) verhindern Übernehmen und Starten; doppelklicken "
              "Sie einen, um sein Teil zu wählen."),
             ("Fix every error.", "Beheben Sie jeden Fehler."), "board-no-errors"),
        task("board", "dry_button", ("Ask the launcher", "Den Launcher fragen"),
             ("A dry run of exactly this board; the verdict under the command line follows every change too.",
              "Ein Probelauf genau dieser Tafel; das Urteil unter der Befehlszeile folgt auch jeder Änderung."),
             ("Press Dry run.", "Drücken Sie Probelauf.")),
        task("board", "apply_button", ("Apply", "Übernehmen"),
             ("Writes .sandbox-ai.json into the project after showing it, and trusts exactly that.",
              "Schreibt .sandbox-ai.json ins Projekt, nachdem es sie gezeigt hat, und vertraut genau dieser."),
             ("Press Apply… and confirm.", "Drücken Sie Übernehmen… und bestätigen Sie."), "board-applied"),
        task("board", "gallery_button", ("Save it as your preset", "Als Ihre Vorlage speichern"),
             ("New from preset… opens the gallery. Save the board as a preset… keeps this board under a title of "
              "yours, without workspaces and secrets, under My presets.",
              "Neu aus Vorlage… öffnet die Galerie. Tafel als Vorlage speichern… bewahrt diese Tafel unter einem "
              "Titel Ihrer Wahl auf, ohne Arbeitsordner und Geheimnisse, unter Meine Vorlagen."),
             ("Open the gallery and save the board as a preset.", "Öffnen Sie die Galerie und speichern Sie die Tafel"
                                                                  " als Vorlage."), "preset-mine"),
        task("board", "gallery_button", ("Start from your preset", "Von Ihrer Vorlage starten"),
             ("In any project, open the gallery, select your preset under My presets and press Create: the board "
              "is built from it. Then Start.",
              "Öffnen Sie in einem beliebigen Projekt die Galerie, wählen Sie Ihre Vorlage unter Meine Vorlagen und "
              "drücken Sie Anlegen: Die Tafel wird daraus gebaut. Dann Start."),
             ("Create a board from your preset and start it.", "Legen Sie eine Tafel aus Ihrer Vorlage an und starten"
                                                               " Sie sie.")),
    )),
    TaskTour("approvals-network", Text("Approvals and network: allow a host during a run",
                                       "Freigaben und Netzwerk: einen Host während eines Laufs erlauben"),
             Text("What happens when a running agent wants a host that is not allowed, how you answer, and how "
                  "you change the network of a running sandbox.",
                  "Was passiert, wenn ein laufender Agent einen nicht erlaubten Host will, wie Sie antworten und "
                  "wie Sie das Netzwerk einer laufenden Sandbox ändern."), (
        task("launch", "form", ("Ask instead of refusing", "Fragen statt abweisen"),
             ("With --ask (on by default in most security modes) a connection that is not allowed waits for your "
              "answer instead of failing.",
              "Mit --ask (in den meisten Sicherheitsmodi Standard) wartet eine nicht erlaubte Verbindung auf Ihre "
              "Antwort, statt zu scheitern."),
             ("Start an agent and ask it to fetch a page from a new host.", "Starten Sie einen Agenten und bitten "
              "Sie ihn, eine Seite von einem neuen Host zu laden.")),
        task("approvals", "open", ("The question", "Die Frage"),
             ("The question shows the sandbox, the host and how long it still waits. Without an answer in time it "
              "is refused.",
              "Die Frage zeigt die Sandbox, den Host und wie lange sie noch wartet. Ohne Antwort rechtzeitig wird "
              "sie abgelehnt."),
             ("Select the question.", "Wählen Sie die Frage."), "page:approvals"),
        task("approvals", "scope", ("For how long", "Für wie lange"),
             ("Once, for the run, or always for the project. Choose once while you are unsure.",
              "Einmal, für den Lauf oder immer für das Projekt. Wählen Sie einmal, solange Sie unsicher sind."),
             ("Choose for the run.", "Wählen Sie für den Lauf.")),
        task("approvals", "allow_button", ("Answer", "Antworten"),
             ("Allow lets it through. Deny for this run refuses it until the run ends, Deny once only this "
              "request, Block always… at every later start too.",
              "Erlauben lässt es durch. Für diesen Lauf ablehnen weist es bis zum Ende des Laufs ab, Einmal ablehnen "
              "nur diese Anfrage, Immer sperren… auch bei jedem späteren Start."),
             ("Allow it.", "Erlauben Sie es.")),
        task("sandboxes", "table", ("Change a running sandbox", "Eine laufende Sandbox ändern"),
             ("Without waiting for a question: select a running sandbox; While it runs allows or revokes a host "
              "until it ends (sandbox-ai net allow).",
              "Ohne auf eine Frage zu warten: Wählen Sie eine laufende Sandbox; Während sie läuft erlaubt oder "
              "entzieht einen Host, bis sie endet (sandbox-ai net allow)."),
             ("Allow a host for a running sandbox.", "Erlauben Sie einen Host für eine laufende Sandbox."),
             "page:sandboxes"),
        task("connections", "refused", ("What was refused", "Was abgewiesen wurde"),
             ("Each refused connection comes with the option that would allow it, so the next start needs no "
              "question.",
              "Jede abgelehnte Verbindung kommt mit der Option, die sie erlauben würde, damit der nächste Start keine"
              " Frage braucht."),
             ("Switch on only refused.", "Schalten Sie nur Abgelehntes und Fehler ein."), "page:connections"),
        task("approvals", "always", ("Standing answers", "Dauerhafte Antworten"),
             ("What you allowed always, per project. Forget one and it is asked again.",
              "Was Sie immer erlaubt haben, pro Projekt. Vergessen Sie eine, wird wieder gefragt."),
             ("Look through them.", "Sehen Sie sie durch."), "page:approvals"),
    )),
    TaskTour("presets-gallery", Text("The preset gallery: import, edit and delete your own",
                                     "Die Vorlagen-Galerie: eigene importieren, bearbeiten und löschen"),
             Text("Find a preset, import one from a file, change one of yours, pass it on and delete it.",
                  "Eine Vorlage finden, eine aus einer Datei importieren, eine Ihrer eigenen ändern, weitergeben "
                  "und löschen."), (
        task("board", "gallery_button", ("Open the gallery", "Die Galerie öffnen"),
             ("New from preset… shows every preset as a card: simple and complex ones, sandbox-ai's own and yours. "
              "The search box filters (vpn, mcp, offline, llm, gpu).",
              "Neu aus Vorlage… zeigt jede Vorlage als Karte: einfache und komplexe, die Vorlagen von sandbox-ai und "
              "Ihre eigenen. Das Suchfeld filtert (vpn, mcp, offline, llm, gpu)."),
             ("Open it and search for offline.", "Öffnen Sie sie und suchen Sie nach offline.")),
        task("board", "gallery_button", ("Import", "Importieren"),
             ("Import… adds a preset from a JSON file to My presets. The file is checked: options by the "
              "launcher's parser, servers and rules by sandbox-ai; anything that looks like a secret is refused. "
              "You can also drop the file onto the board.",
              "Importieren… fügt eine Vorlage aus einer JSON-Datei zu Meine Vorlagen hinzu. Die Datei wird geprüft: "
              "Optionen vom Parser des Launchers, Server und Regeln von sandbox-ai; alles, was wie ein Geheimnis "
              "aussieht, wird abgelehnt. Sie können die Datei auch auf die Tafel ziehen."),
             ("Import a preset file someone gave you.",
              "Importieren Sie eine Vorlagen-Datei, die Sie bekommen haben."), "preset-mine"),
        task("board", "view", ("Edit", "Bearbeiten"),
             ("A preset is edited on the board: create a board from it, change it, then save the board as a preset "
              "under the same title. The new one replaces the old one.",
              "Eine Vorlage wird auf der Tafel bearbeitet: Legen Sie daraus eine Tafel an, ändern Sie sie und "
              "speichern Sie die Tafel unter demselben Titel als Vorlage. Die neue ersetzt die alte."),
             ("Create a board from your preset, add a part, save it under the same title.",
              "Legen Sie eine Tafel aus Ihrer Vorlage an, fügen Sie ein Teil hinzu, speichern Sie sie unter demselben"
              " Titel.")),
        task("board", "gallery_button", ("Export", "Exportieren"),
             ("Export… writes the selected preset to a JSON file to pass on; it is checked once more so nothing "
              "secret leaves.",
              "Exportieren… schreibt die gewählte Vorlage in eine JSON-Datei zum Weitergeben; sie wird noch einmal "
              "geprüft, damit nichts Geheimes hinausgeht."),
             ("Export one of yours.", "Exportieren Sie eines Ihrer eigenen.")),
        task("board", "gallery_button", ("Delete", "Löschen"),
             ("Delete removes the selected preset, after asking; only your own can be deleted.",
              "Löschen entfernt die gewählte Vorlage nach Rückfrage; nur Ihre eigenen lassen sich löschen."),
             ("Delete a preset you no longer need.", "Löschen Sie eine Vorlage, die Sie nicht mehr brauchen.")),
        task("board", "gallery_button", ("sandbox-ai's presets", "Die Vorlagen von sandbox-ai"),
             ("The section sandbox-ai's presets lists the launcher's own presets (sandbox-ai presets list); on "
              "the command line a start uses one with --preset NAME.",
              "Der Abschnitt mit den Vorlagen von sandbox-ai zeigt die eigenen Vorlagen des Launchers (sandbox-ai "
              "presets list); auf der Befehlszeile nutzt ein Start eines mit --preset NAME."),
             ("Look at one of them.", "Sehen Sie sich eines davon an.")),
    )),
    TaskTour("help-and-tips", Text("The Help area and the (?) buttons", "Die Hilfe und die (?)-Knöpfe"),
             Text("Where every explanation is: the (?) next to each area and part, the Help page with every topic "
                  "in English and German, and the tours.",
                  "Wo jede Erklärung ist: das (?) neben jedem Bereich und Teil, die Seite Hilfe mit jedem Thema auf "
                  "Englisch und Deutsch und die Touren."), (
        task("board", "view", ("A (?) everywhere", "Überall ein (?)"),
             ("Each page, area and board part has a small (?). It says what the thing is, what it is for, what "
              "happens when you use it, and the command-line option behind it.",
              "Jede Seite, jeder Bereich und jedes Teil der Tafel hat ein kleines (?). Es sagt, was das Ding ist, "
              "wozu es dient, was passiert, wenn Sie es benutzen, und welche Option der Befehlszeile dahintersteht."),
             ("Click the (?) in the corner of the board, or the one in a node's title band.",
              "Klicken Sie das (?) in der Ecke der Tafel oder das im Titelband eines Knotens."), "tip-opened"),
        task("help", "index", ("More in Help", "Mehr in der Hilfe"),
             ("More in Help in an explanation opens this page at the matching section.",
              "Mehr in der Hilfe in einer Erklärung öffnet diese Seite beim passenden Abschnitt."),
             ("Pick a section on the left.", "Wählen Sie links einen Abschnitt."), "page:help"),
        task("help", "search", ("Search", "Suchen"),
             ("Type words: sections with all of them remain, in either language.",
              "Geben Sie Wörter ein: Abschnitte mit allen bleiben übrig, in beiden Sprachen."),
             ("Search for canary.", "Suchen Sie nach canary."), "help-search"),
        task("help", "language", ("Language", "Sprache"),
             ("The help follows the app's language; switch it here at any time.",
              "Die Hilfe folgt der Sprache der App; wechseln Sie sie hier jederzeit."),
             ("Read a section in the other language.", "Lesen Sie einen Abschnitt in der anderen Sprache.")),
        task("help", "tours", ("Tours", "Touren"),
             ("Start any tour from here; F1 starts the tour of the page you are on.",
              "Starten Sie hier jede Tour; F1 startet die Tour der Seite, auf der Sie sind."),
             ("Start the tour of your own construct.", "Starten Sie die Tour zum eigenen Konstrukt.")),
    )),
)}


def task_tour(ident):
    """The TaskTour IDENT (KeyError for an unknown one)."""
    return TASK_TOURS[ident]
