"""The Help area's sections: every page and feature of the app and the platform's concepts, in English and German.
No Qt. help_view.py shows them; tests check that every section has both languages."""
from __future__ import annotations

from dataclasses import dataclass

from .tours import Text


@dataclass(frozen=True)
class Section:
    id: str
    group: str        # "app" (pages and features of the app) or "concepts" (sandbox-ai itself)
    title: Text
    body: Text        # paragraphs separated by a blank line ("\n\n"); a line starting "- " is a bullet; `x` is code
    cli: tuple = ()   # related commands/options, shown as they are (same in both languages)
    see: tuple = ()   # ids of related sections
    tours: tuple = ()  # tours to start from here (ids listed below)


def section(id, group, title, body, cli=(), see=(), tours=()):  # noqa: A002 - the field's name
    return Section(id, group, Text(*title), Text(*body), tuple(cli), tuple(see), tuple(tours))


SECTIONS = (
    # -- the pages of the app --
    section(
        "page-launch", "app", ("Start page", "Seite Start"),
        ("The Start page starts one agent in one sandbox. The current project's folder is the first workspace "
         "(`/work`). In the row above the form:\n\n"
         "- Host: this machine or a host of `sandbox-ai hosts`. On another host the start runs there over SSH (the "
         "command line shows `sandbox-ai hosts run HOST -- …`): workspace and mount paths are that host's and are "
         "typed in. The options its sandbox-ai does not know are greyed out; a start that needs one of them is "
         "refused with their names, never started without them.\n"
         "- Agent (Claude Code, Codex or OpenCode) and Model: the agent's own provider, or a model server "
         "registered on the chosen host (`sandbox-ai llm list` there), as NAME or NAME:MODEL. A server sets "
         "`--llm` and leaves `--model` empty; only this server adds `--no-provider-defaults`. A server the agent "
         "cannot use is shown, greyed out.\n"
         "- Preset, with Gallery… beside it: the presets of the chosen host in one list - that host's sandbox-ai "
         "presets (`sandbox-ai presets list --json` there), the app's own and yours. When the app and the host's "
         "sandbox-ai both have a preset of one id, the host's is used. Gallery… shows each with a picture of its "
         "board; what a preset leaves to fill in is asked in a short form that offers that host's VPN profiles, "
         "LLM servers and MCP servers.\n"
         "- Profile with Load and Save as profile… keeps a whole form (host, agent, options, arguments) under a "
         "name. Open project folder… reviews a folder's `.sandbox-ai.json`, trusts it if you agree and starts in "
         "that folder.\n\n"
         "The form is built from the launcher's own option parser, in groups: workspace, network, proxy and "
         "privacy, Git, agent, login, display and terminal, hardware, limits, engine. What the administrator's "
         "policy forbids is greyed out. `--vpn`, `--kit`, `--identity`, `--mcp` and `--image-variant` are "
         "choosers filled from the chosen host's lists (`vpn list`, `kits list`, `identities list`, `mcp list` "
         "and `images` there). `--gpu` offers the kinds this computer has, and every kind for another host (its "
         "launcher checks it); the memory and CPU hints name the host they are for.\n\n"
         "- Below it are the agent arguments and the exact command line the app will run. Copy it into a terminal "
         "to start the same sandbox there.\n"
         "- Dry run lets the launcher of the chosen host check everything and show its plan, without starting "
         "anything.\n"
         "- Start in the app opens a terminal tab, Start in a terminal window uses your terminal program, Start as "
         "session starts a session that keeps running when you close the app.\n\n"
         "Save as project configuration… writes `.sandbox-ai.json` into the first workspace and trusts it, after "
         "showing it and asking first (Cancel is the default: a trusted file gives every later start of the "
         "project its options). On another host it is off: a project's file there is written with `sandbox-ai "
         "wizard`. When the project's file is not trusted, Review and trust… shows it first.\n\n"
         "The page remembers the host, the agent, the profile and, per agent, the last options. Password fields are "
         "never kept, and agent arguments lose what looks like a secret. Press Dry run once before your first "
         "start.",
         "Die Seite Start startet einen Agenten in einer Sandbox. Der Ordner des aktuellen Projekts ist der erste "
         "Arbeitsordner (`/work`). In der Zeile über dem Formular:\n\n"
         "- Host: dieser Rechner oder ein Host von `sandbox-ai hosts`. Auf einem anderen Host läuft der Start dort "
         "über SSH (die Befehlszeile zeigt `sandbox-ai hosts run HOST -- …`): Arbeitsordner- und Mount-Pfade sind die "
         "dieses Hosts und werden eingetippt. Die Optionen, die sein sandbox-ai nicht kennt, sind ausgegraut; ein "
         "Start, der eine davon braucht, wird mit ihren Namen abgelehnt, nie ohne sie gestartet.\n"
         "- Agent (Claude Code, Codex oder OpenCode) und Modell: der eigene Anbieter des Agenten oder ein auf dem "
         "gewählten Host registrierter Modellserver (`sandbox-ai llm list` dort), als NAME oder NAME:MODELL. Ein "
         "Server setzt `--llm` und lässt `--model` leer; nur dieser Server fügt `--no-provider-defaults` hinzu. "
         "Ein Server, den der Agent nicht nutzen kann, wird ausgegraut gezeigt.\n"
         "- Vorlage, mit Galerie… daneben: die Vorlagen des gewählten Hosts in einer Liste - die Vorlagen von "
         "sandbox-ai auf diesem Host (`sandbox-ai presets list --json` dort), die eigenen der App und Ihre. Haben "
         "die App und das sandbox-ai des Hosts eine Vorlage mit derselben ID, gilt die des Hosts. Galerie… zeigt "
         "jede mit einem Bild ihrer Tafel; was eine Vorlage auszufüllen lässt, fragt ein kurzes Formular ab, das "
         "die VPN-Profile, LLM-Server und MCP-Server dieses Hosts anbietet.\n"
         "- Profil mit Laden und Als Profil speichern… bewahrt ein ganzes Formular (Host, Agent, Optionen, "
         "Argumente) unter einem Namen auf. Projektordner öffnen… prüft die `.sandbox-ai.json` eines Ordners, "
         "vertraut ihr, wenn Sie zustimmen, und startet in diesem Ordner.\n\n"
         "Das Formular entsteht aus dem Optionsparser des Launchers, in Gruppen: Arbeitsordner, Netzwerk, Proxy und "
         "Datenschutz, Git, Agent, Anmeldung, Anzeige und Terminal, Hardware, Limits, Engine. Was die Richtlinie "
         "des Administrators verbietet, ist ausgegraut. `--vpn`, `--kit`, `--identity`, `--mcp` und "
         "`--image-variant` sind Auswahllisten, gefüllt aus den Listen des gewählten Hosts (`vpn list`, `kits "
         "list`, `identities list`, `mcp list` und `images` dort). `--gpu` bietet die Arten an, die dieser Rechner "
         "hat, und für einen anderen Host jede Art (sein Launcher prüft sie); die Hinweise zu Speicher und CPUs "
         "nennen den Host, für den sie gelten.\n\n"
         "- Darunter stehen die Argumente des Agenten und die genaue Befehlszeile, die die App ausführt. Kopieren "
         "Sie sie in ein Terminal, um dort dieselbe Sandbox zu starten.\n"
         "- Probelauf lässt den Launcher des gewählten Hosts alles prüfen und seinen Plan zeigen, ohne etwas zu "
         "starten.\n"
         "- In der App starten öffnet einen Terminal-Tab, In einem Terminalfenster starten nutzt Ihr "
         "Terminalprogramm, Als Sitzung starten startet eine Sitzung, die weiterläuft, wenn Sie die App "
         "schließen.\n\n"
         "Als Projektkonfiguration speichern… schreibt `.sandbox-ai.json` in den ersten Arbeitsordner und vertraut "
         "ihr, nachdem es sie gezeigt und zuerst gefragt hat (Abbrechen ist die Vorgabe: Eine vertraute Datei gibt "
         "jedem späteren Start des Projekts ihre Optionen). Auf einem anderen Host ist es aus: Die Datei eines "
         "Projekts dort schreibt `sandbox-ai wizard`. Ist die Datei des Projekts nicht vertraut, zeigt Prüfen und "
         "vertrauen… sie zuerst.\n\n"
         "Die Seite merkt sich Host, Agent, Profil und je Agent die letzten Optionen. Passwortfelder werden nie "
         "gespeichert, und Agent-Argumente verlieren, was wie ein Geheimnis aussieht. Drücken Sie vor Ihrem ersten "
         "Start einmal Probelauf."),
        cli=("sandbox-ai claude", "sandbox-ai codex", "sandbox-ai opencode", "--dry-run", "--workspace", "--llm",
             "--preset", "sandbox-ai presets list --json", "sandbox-ai hosts run HOST --"),
        see=("sandbox", "security-modes", "network", "sessions", "page-board", "hosts", "llm",
             "app-presets-gallery"),
        tours=("first-start", "area:launch")),
    section(
        "page-board", "app", ("Board", "Tafel"),
        ("The Board shows one sandbox as parts you connect: the agent, workspaces, mounts, HOME and login, kits, "
         "approvals, the connection proxy, Git, MCP servers, LLM servers, VPN, rulesets, network, devices, limits, "
         "display, session, logs and engine. It writes the project's `.sandbox-ai.json`; the layout goes to "
         "`.sandbox-ai.board.json` beside it.\n\n"
         "- Drag a part from the parts list onto the board: it lands where you drop it (a dashed card shows "
         "where). Double-click or Enter adds it too. The guide then asks the few things it needs, each with what "
         "it means. Set up step by step… in the inspector asks again for a part already on the board. A part a "
         "board has once is greyed out while it is there.\n"
         "- Drag from a part's right port onto another part to connect them; the parts that can take the "
         "connection are framed, Esc cancels. A workspace, mount or MCP server counts only while it is connected "
         "to the agent.\n"
         "- Drop folders or files from your file manager onto the board: a menu offers workspace, read-only or "
         "writable mount, or the folder of the part you dropped on. A `.sandbox-ai.json` (or a folder with one) "
         "opens its project; a preset file of the app is imported. Every change is one step of Undo.\n"
         "- The inspector edits the selected part with the launcher's own options. Findings, security badges and "
         "a score, the command line and the launcher's own verdict (a dry run in the background) follow every "
         "change. Click a finding to see its part.\n"
         "- Apply… shows the file and what changes and asks first (Cancel is the default), then writes and trusts "
         "it. Dry run, Start, Undo and Redo are there too.\n\n"
         "New from preset… opens the preset gallery of the board's host (its presets, LLM servers, VPN profiles "
         "and MCP servers), Wizard… builds the board step by step. Each project keeps "
         "its own board, with changes not applied yet, the undo history and the zoom. After a crash such changes "
         "come back with a notice: Undo returns to the project's file, Discard drops them. The Devices and Limits "
         "parts of a project whose sandbox runs can be applied live. Empty the findings before you apply.",
         "Die Tafel zeigt eine Sandbox als Teile, die Sie verbinden: Agent, Arbeitsordner, Mounts, HOME und "
         "Anmeldung, Kits, Freigaben, Verbindungs-Proxy, Git, MCP-Server, LLM-Server, VPN, Regelwerke, Netzwerk, "
         "Geräte, Limits, Anzeige, Sitzung, Protokolle und Engine. Sie schreibt die `.sandbox-ai.json` des Projekts; "
         "die Anordnung kommt in `.sandbox-ai.board.json` daneben.\n\n"
         "- Ziehen Sie ein Teil aus der Liste der Bausteine auf die Tafel: Es landet, wo Sie es ablegen (eine "
         "gestrichelte Karte zeigt, wo). Doppelklick oder Enter fügt es ebenfalls hinzu. Der Guide fragt dann das "
         "Wenige, das es braucht, und erklärt jede Frage. Schritt für Schritt einrichten… im Inspektor fragt für "
         "ein Teil auf der Tafel erneut. Ein Teil, das eine Tafel nur einmal hat, ist ausgegraut, solange es da "
         "ist.\n"
         "- Ziehen Sie vom rechten Anschluss eines Teils auf ein anderes, um sie zu verbinden; die Teile, die die "
         "Verbindung annehmen, werden umrandet, Esc bricht ab. Ein Arbeitsordner, Mount oder MCP-Server zählt nur, "
         "solange er mit dem Agenten verbunden ist.\n"
         "- Legen Sie Ordner oder Dateien aus Ihrem Dateimanager auf der Tafel ab: Ein Menü bietet Arbeitsordner, "
         "nur lesbare oder beschreibbare Einbindung oder den Ordner des Teils an, auf dem Sie abgelegt haben. "
         "Eine `.sandbox-ai.json` (oder ein Ordner mit einer) öffnet ihr Projekt; eine Vorlagen-Datei der App wird "
         "importiert. Jede Änderung ist ein Schritt von Rückgängig.\n"
         "- Der Inspektor bearbeitet das gewählte Teil mit den Optionen des Launchers. Befunde, "
         "Sicherheitsabzeichen und eine Wertung, die Befehlszeile und das Urteil des Launchers (ein Probelauf im "
         "Hintergrund) folgen jeder Änderung. Klicken Sie einen Befund an, um sein Teil zu sehen.\n"
         "- Übernehmen… zeigt die Datei und was sich ändert und fragt zuerst (Abbrechen ist die Vorgabe), schreibt "
         "sie dann und vertraut ihr. Probelauf, Start, Rückgängig und Wiederholen gibt es auch.\n\n"
         "Neu aus Vorlage… öffnet die Vorlagen-Galerie des Hosts der Tafel (seine Vorlagen, LLM-Server, "
         "VPN-Profile und MCP-Server), Assistent… baut die Tafel Schritt für Schritt. Jedes "
         "Projekt behält seine eigene Tafel samt nicht übernommener Änderungen, Rückgängig-Verlauf und Zoom. Nach "
         "einem Absturz kommen solche Änderungen mit einem Hinweis zurück: Rückgängig kehrt zur Datei des Projekts "
         "zurück, Verwerfen verwirft sie. Die Teile Geräte und Limits eines Projekts, dessen Sandbox läuft, lassen "
         "sich sofort anwenden. Leeren Sie die Befunde, bevor Sie übernehmen."),
        cli=("sandbox-ai wizard trust FILE", "--config", "--no-config"),
        see=("app-presets-gallery", "app-wizard", "workspaces-mounts", "rulesets", "page-launch"),
        tours=("own-construct", "area:board")),
    section(
        "page-sandboxes", "app", ("Sandboxes & sessions", "Sandboxen & Sitzungen"),
        ("This page lists every sandbox-ai instance of your account as a tree, read again every few seconds: "
         "each host (how many starts, how many run, whether it answers), under it its starts - one-off runs "
         "(started here, in a terminal, by a script or another copy of the app), sessions (running, paused or "
         "stopped), MCP server sandboxes, and launcher processes that have not registered yet - and under each "
         "start what belongs to it: the MCP servers and helpers it reaches and its Podman pod; under an MCP server "
         "the starts it serves. This machine and every host of `sandbox-ai hosts` are listed. Columns show kind, "
         "name, project, agent, backend, start time, state, proxy, host and memory. The current project's rows are "
         "bold; Only the current project hides the rest, Ended also lists the starts that ended (`sandbox-ai list "
         "--all`). Host (shown once there are hosts) lists all of them, this machine's or one host's; Show starts "
         "on the Hosts page sets it. Which groups are open is kept; View › Fold all sections and Unfold all "
         "sections close or open them all, and a double-click on a row below a start opens what it stands for.\n\n"
         "The hosts are read about every 10 seconds while the page is shown and every minute while it is not; "
         "Refresh reads again now. The rows of a host that did not answer are greyed with “last seen N s ago”, "
         "and their buttons are off: what they show may no longer be true. A start with an open question is "
         "marked asking.\n\n"
         "The buttons act on the selected start row and follow its kind; a tooltip says why one does not apply.\n\n"
         "- Session: Attach (in a window), Attach in the app, Pause, Resume, Delete. Resume with… starts a kept "
         "start again with workspaces, mounts or MCP servers added (kept for its later starts), or restarts a "
         "running one now (`sandbox-ai resume`); a restart and every widening are asked first.\n"
         "- While it runs: Allow… and Revoke… change what the sandbox may reach, Allowed during the run shows "
         "what was added (Untaint… there lifts the run's taint), Connection log opens its log, Add device…, Remove "
         "device and Limits… change hardware and limits until it ends.\n"
         "- Add workspace… mounts a folder or a single file of the start's host at `/workspaces/NAME` (the name "
         "is optional; checked as `--name`), read-only or writable (a writable one is asked first). A session "
         "keeps a folder for its restart unless it is read-only, a file only for this run. Remove workspace… takes "
         "one back.\n"
         "- Add MCP server… gives the selected kept start a server of its host's `sandbox-ai mcp list`, or another "
         "agent as a helper, now, without a restart (`sandbox-ai mcp attach --live`), and keeps it for the start's "
         "later starts. It asks first. Where the agent needs a restart to see the server (Codex and OpenCode "
         "always, Claude Code when /mcp does not list it), the app offers to restart it now: the agent continues "
         "its conversation. Remove MCP server… takes a server away now and from its later starts (`sandbox-ai mcp "
         "detach --live`), after asking.\n"
         "- On another host, what to take back (a device, a workspace, a server) is chosen from what that host "
         "lists for the start.\n"
         "- Rename… gives a start another name (its run id stays, and so do its row and its tab), History… shows "
         "its starts, ends, pauses and renames. Clean up… removes leftovers and kept starts unused for longer than "
         "keep_days, on this machine or the host chosen; it shows what it would remove first and asks, and removes "
         "only that: when the list changed meanwhile, nothing is removed. While a command runs, these buttons "
         "wait.\n"
         "- Stop… asks first. A session keeps its HOME; a one-off run's launcher cleans up.\n\n"
         "A launcher that has not registered can only be ended in its own terminal. Connections already open "
         "stay open after a revoke.",
         "Diese Seite listet jede Instanz von sandbox-ai Ihres Kontos als Baum, alle paar Sekunden neu gelesen: "
         "jeden Host (wie viele Starts, wie viele laufen, ob er antwortet), darunter seine Starts - einmalige Läufe "
         "(hier, in einem Terminal, von einem Skript oder einer anderen App gestartet), Sitzungen (laufend, "
         "pausiert oder gestoppt), Sandboxen von MCP-Servern und Launcher, die sich noch nicht angemeldet haben - "
         "und unter jedem Start, was zu ihm gehört: die MCP-Server und Helfer, die er erreicht, und sein "
         "Podman-Pod; unter einem MCP-Server die Starts, die er bedient. Dieser Rechner und jeder Host von "
         "`sandbox-ai hosts` werden gelistet. Die Spalten zeigen Art, Name, Projekt, Agent, Backend, Startzeit, "
         "Zustand, Proxy, Host und Speicher. Die Zeilen des aktuellen Projekts sind fett; Nur das aktuelle Projekt "
         "blendet den Rest aus, Beendete listet auch die beendeten Starts (`sandbox-ai list --all`). Host "
         "(sichtbar, sobald es Hosts gibt) listet alle, die dieses Rechners oder die eines Hosts; Starts zeigen "
         "auf der Seite Hosts stellt es ein. Welche Gruppen offen sind, bleibt erhalten; Ansicht › Alle Abschnitte "
         "einklappen und Alle Abschnitte ausklappen schließen oder öffnen alle, und ein Doppelklick auf eine Zeile "
         "unter einem Start öffnet, wofür sie steht.\n\n"
         "Die Hosts werden etwa alle 10 Sekunden gelesen, solange die Seite zu sehen ist, sonst jede Minute; Neu "
         "laden liest sofort neu. Die Zeilen eines Hosts, der nicht antwortete, sind ausgegraut mit „zuletzt "
         "gesehen vor N s“, und ihre Schaltflächen sind aus: Was sie zeigen, stimmt vielleicht nicht mehr. Ein "
         "Start mit offener Frage ist als fragt markiert.\n\n"
         "Die Schaltflächen wirken auf die gewählte Zeile eines Starts und folgen ihrer Art; ein Tooltip sagt, "
         "warum eine nicht passt.\n\n"
         "- Sitzung: Verbinden (in einem Fenster), In der App verbinden, Pausieren, Fortsetzen, Löschen. "
         "Fortsetzen mit … startet einen aufbewahrten Start erneut mit hinzugefügten Arbeitsordnern, Mounts oder "
         "MCP-Servern (für seine späteren Starts behalten) oder startet einen laufenden sofort neu (`sandbox-ai "
         "resume`); ein Neustart und jede Erweiterung werden zuerst gefragt.\n"
         "- Während sie läuft: Erlauben… und Zurücknehmen… ändern, was die Sandbox erreichen darf, Während des "
         "Laufs erlaubt zeigt das Hinzugefügte (Taint aufheben… dort hebt den Taint des Laufs auf), Verbindungslog "
         "öffnet ihr Protokoll, Gerät hinzufügen…, Gerät entfernen und Grenzen… ändern Hardware und Limits, bis sie "
         "endet.\n"
         "- Arbeitsordner hinzufügen… bindet einen Ordner oder eine einzelne Datei des Hosts des Starts unter "
         "`/workspaces/NAME` ein (der Name ist freiwillig; geprüft wie `--name`), nur lesbar oder beschreibbar "
         "(ein beschreibbarer wird zuerst gefragt). Eine Sitzung behält einen Ordner für ihren Neustart, außer er "
         "ist nur lesbar, eine Datei nur für diesen Lauf. Arbeitsordner entfernen… nimmt einen zurück.\n"
         "- MCP-Server hinzufügen… gibt dem gewählten aufbewahrten Start einen Server aus `sandbox-ai mcp list` "
         "seines Hosts oder einen anderen Agenten als Helfer, sofort und ohne Neustart (`sandbox-ai mcp attach "
         "--live`), und behält ihn für die späteren Starts. Es fragt zuerst. Braucht der Agent einen Neustart, um "
         "den Server zu sehen (Codex und OpenCode immer, Claude Code, wenn /mcp ihn nicht listet), bietet die App "
         "an, ihn sofort neu zu starten: Der Agent setzt sein Gespräch fort. MCP-Server entfernen… nimmt einen "
         "Server sofort und für die späteren Starts weg (`sandbox-ai mcp detach --live`), nach Rückfrage.\n"
         "- Auf einem anderen Host wird, was zurückgenommen wird (ein Gerät, ein Arbeitsordner, ein Server), aus dem "
         "gewählt, was dieser Host für den Start auflistet.\n"
         "- Umbenennen… gibt einem Start einen anderen Namen (seine Lauf-ID bleibt, und mit ihr seine Zeile und "
         "sein Tab), Verlauf… zeigt seine Starts, Enden, Pausen und Umbenennungen. Aufräumen… entfernt Reste und "
         "aufbewahrte Starts, die länger als keep_days ungenutzt sind, auf diesem Rechner oder dem gewählten Host; "
         "es zeigt zuerst, was es entfernen würde, fragt und entfernt nur das: Hat sich die Liste inzwischen "
         "geändert, wird nichts entfernt. Solange ein Befehl läuft, warten diese Knöpfe.\n"
         "- Stoppen… fragt zuerst. Eine Sitzung behält ihr HOME; der Launcher eines einmaligen Laufs räumt auf.\n\n"
         "Ein nicht angemeldeter Launcher lässt sich nur in seinem eigenen Terminal beenden. Schon offene "
         "Verbindungen bleiben nach dem Zurücknehmen offen."),
        cli=("sandbox-ai list", "sandbox-ai list --all", "sandbox-ai list --history NAME", "sandbox-ai rename OLD NEW",
             "sandbox-ai gc --dry-run", "sandbox-ai net list", "sandbox-ai net allow", "sandbox-ai net revoke",
             "sandbox-ai net untaint RUN", "sandbox-ai net stop RUN", "sandbox-ai session stop NAME",
             "sandbox-ai mcp stop NAME", "sandbox-ai devices", "sandbox-ai limits", "sandbox-ai workspace add",
             "sandbox-ai mcp attach NAME START --live", "sandbox-ai mcp detach NAME START --live",
             "sandbox-ai resume NAME"),
        see=("sessions", "network", "taint", "devices", "limits", "page-hosts", "page-terminals", "mcp",
             "workspaces-mounts"),
        tours=("area:sandboxes",)),
    section(
        "page-approvals", "app", ("Approvals", "Freigaben"),
        ("A running sandbox waits here when its agent wants something the options do not allow: a connection, a "
         "name lookup, a push despite privacy warnings, an MCP tool call or request an ask rule holds, a URL to "
         "open. The questions of this machine and of every host of `sandbox-ai hosts` are listed together; "
         "Questions of shows only this machine's or one host's (Show questions on the Hosts page sets it). Each "
         "question shows the run, what is asked, its age, the countdown and the host: without an answer it is "
         "refused after 60 seconds - a page to open is not: it waits until it is answered. A host that stops "
         "answering keeps its questions listed for a minute after its last answer, and an answer sent to a host "
         "stays marked answered (no second answer) until that host no longer lists the question.\n\n"
         "- Choose the scope of an allow: once, for this run, or always (this project). The security mode and the "
         "administrator's max_approval_scope may offer less.\n"
         "- Allow (Ctrl+Return) asks for a confirmation that names the sandbox, the question and the scope (Cancel is "
         "the default for always). For another host's page to open it reads Open here and allow: the page opens "
         "in this computer's browser, and the host is told not to open it (`--no-open`).\n"
         "- Deny for this run (Ctrl+Shift+D) is the default deny: refused, and not asked again until the run "
         "ends. Deny once refuses only this time, so the sandbox may ask again. Block always… (Ctrl+Shift+K) "
         "refuses it at every later start of the project too, after asking you. The keys work while the focus is "
         "in the open questions, and in the window of questions that opens above another dialog.\n"
         "- Each question is answered on its own: several answers can be on their way at once, and a slow host "
         "never holds up the others.\n"
         "- The selected question explains itself from what is already known, never by looking anything up: a "
         "name the agent gave is marked unverified.\n\n"
         "History lists questions with answers, of this machine or of the host chosen in History of (as its "
         "`sandbox-ai approvals history` says them). Allowed or blocked always (per project) lists your standing "
         "answers, of this machine or of a host chosen beside Refresh; Forget… removes one there, so it is asked "
         "again. Sandboxes running now keep what they were allowed until they end: use Revoke… on Sandboxes & "
         "sessions there.\n\n"
         "The page is polled every second, also while the window is closed to the tray; new questions raise a "
         "desktop notification each and a count in the navigation. The open questions are a panel you can float over "
         "other pages. Choose once while you are unsure.",
         "Eine laufende Sandbox wartet hier, wenn ihr Agent etwas will, das die Optionen nicht erlauben: eine "
         "Verbindung, eine Namensauflösung, einen Push trotz Datenschutz-Warnungen, einen MCP-Aufruf oder eine "
         "Anfrage, die eine ask-Regel anhält, eine zu öffnende URL. Die Fragen dieses Rechners und jedes Hosts von "
         "`sandbox-ai hosts` stehen zusammen; Fragen von zeigt nur die dieses Rechners oder eines Hosts (Fragen "
         "zeigen auf der Seite Hosts stellt es ein). Jede Frage zeigt Lauf, Gegenstand, Alter, Countdown und Host: "
         "Ohne Antwort wird sie nach 60 Sekunden abgelehnt - eine zu öffnende Seite nicht: sie wartet, bis sie "
         "beantwortet ist. Ein Host, der nicht mehr antwortet, behält seine Fragen noch eine Minute nach seiner "
         "letzten Antwort in der Liste, und eine an einen Host geschickte Antwort bleibt als beantwortet markiert "
         "(keine zweite Antwort), bis dieser Host die Frage nicht mehr listet.\n\n"
         "- Wählen Sie die Geltung eines Erlaubens: einmal, für diesen Lauf oder immer (dieses Projekt). "
         "Sicherheitsmodus und max_approval_scope des Administrators können weniger anbieten.\n"
         "- Erlauben (Strg+Eingabe) verlangt eine Bestätigung, die Sandbox, Frage und Geltung nennt (bei immer ist "
         "Abbrechen vorgewählt). Für eine zu öffnende Seite eines anderen Hosts heißt es Hier öffnen und erlauben: "
         "Die Seite öffnet sich im Browser dieses Rechners, und der Host erfährt, dass er sie nicht öffnen soll "
         "(`--no-open`).\n"
         "- Für diesen Lauf ablehnen (Strg+Umschalt+D) ist das übliche Ablehnen: abgewiesen und bis zum Ende des "
         "Laufs nicht wieder gefragt. Einmal ablehnen weist nur dieses Mal ab, die Sandbox darf wieder fragen. "
         "Immer sperren… (Strg+Umschalt+K) weist es nach einer Rückfrage auch bei jedem späteren Start des "
         "Projekts ab. Die Tasten wirken, solange der Fokus in den offenen Fragen liegt, und im Fenster der "
         "Fragen, das sich über einem anderen Dialog öffnet.\n"
         "- Jede Frage wird für sich beantwortet: Mehrere Antworten können zugleich unterwegs sein, und ein "
         "langsamer Host hält die anderen nie auf.\n"
         "- Die gewählte Frage erklärt sich aus dem, was schon bekannt ist, ohne etwas nachzuschlagen: Ein Name, "
         "den der Agent angab, ist als ungeprüft markiert.\n\n"
         "Verlauf listet Fragen mit Antworten, die dieses Rechners oder die des unter Verlauf von gewählten Hosts "
         "(wie sein `sandbox-ai approvals history` sie nennt). Immer erlaubt oder blockiert (je Projekt) listet Ihre dauerhaften "
         "Antworten, die dieses Rechners oder eines Hosts, den Sie neben Neu laden wählen; Vergessen… entfernt "
         "dort eine, dann wird wieder gefragt. Jetzt laufende Sandboxen behalten bis zu ihrem Ende, was sie "
         "erlaubt bekamen: Nutzen Sie dort Zurücknehmen… auf Sandboxen & Sitzungen.\n\n"
         "Die Seite fragt jede Sekunde ab, auch wenn das Fenster in den Infobereich geschlossen ist; neue Fragen "
         "bringen je eine Desktop-Benachrichtigung und einen Zähler in der Navigation. Die offenen Fragen sind ein "
         "Bereich, den Sie über anderen Seiten schweben lassen können. Wählen Sie einmal, solange Sie unsicher "
         "sind."),
        cli=("sandbox-ai approvals list", "sandbox-ai approvals allow ID --scope once|run|always [--no-open]",
             "sandbox-ai approvals deny ID --scope run|once|always", "sandbox-ai approvals history",
             "sandbox-ai approvals remembered", "sandbox-ai approvals forget PROJECT ENTRY",
             "sandbox-ai hosts run HOST -- approvals list", "--ask"),
        see=("approvals", "network", "rulesets", "policy", "page-hosts"),
        tours=("approvals-network", "area:approvals")),
    section(
        "page-terminals", "app", ("Terminals", "Terminals"),
        ("Terminals shows terminal tabs inside the app: agents started with Start in the app, sessions attached "
         "in the app, and commands such as builds and logins when Settings say they run in the app. In a session "
         "the agent runs in the tmux window `agent`, a shell in the window `shell`.\n\n"
         "- Ctrl-b d detaches from a session: it keeps running, and Attach on Sandboxes & sessions brings it "
         "back.\n"
         "- On this page Ctrl+W and Ctrl+Tab go to the terminal, not to the app's tabs.\n\n"
         "The tabs use xterm.js in QtWebEngine. xterm.js is not part of the app: it is downloaded once and "
         "checked against its pinned digest (Download xterm.js now, when the page offers it). The page loads "
         "only local files and reaches nothing remote. Without QtWebEngine or xterm.js, commands open in a "
         "terminal window instead, which stays open after the end. The AppImage and the macOS app come without "
         "QtWebEngine.",
         "Terminals zeigt Terminal-Tabs in der App: Agenten, die mit In der App starten gestartet wurden, in der "
         "App verbundene Sitzungen und Befehle wie Builds und Anmeldungen, wenn die Einstellungen sie in der App "
         "laufen lassen. In einer Sitzung läuft der Agent im tmux-Fenster `agent`, eine Shell im Fenster "
         "`shell`.\n\n"
         "- Strg-b d löst sich von einer Sitzung: Sie läuft weiter, und Verbinden auf Sandboxen & Sitzungen holt "
         "sie zurück.\n"
         "- Auf dieser Seite gehen Strg+W und Strg+Tab an das Terminal, nicht an die Tabs der App.\n\n"
         "Die Tabs nutzen xterm.js in QtWebEngine. xterm.js gehört nicht zur App: Es wird einmal geladen und "
         "gegen seinen festgelegten Digest geprüft (xterm.js jetzt herunterladen, wenn die Seite es anbietet). Die "
         "Seite lädt nur lokale Dateien und erreicht nichts im Netz. Ohne QtWebEngine oder xterm.js öffnen Befehle "
         "stattdessen ein Terminalfenster, das nach dem Ende offen bleibt. AppImage und macOS-App kommen ohne "
         "QtWebEngine."),
        cli=("sandbox-ai attach NAME", "--tmux", "--no-tmux"),
        see=("sessions", "page-sandboxes", "app-settings"),
        tours=("area:terminals",)),
    section(
        "page-connections", "app", ("Connections", "Verbindungen"),
        ("Connections shows the connection proxy's log of a project and run: every request, push and refusal. "
         "Pick the log directory of the project you work on; the log follows while the sandbox runs.\n\n"
         "- Filter by host, path or method. Only refused and errors shows what was not let through, in red.\n"
         "- Select a line to see its full record: method, URL, headers and bodies (credentials redacted), the "
         "rule that decided and the answer.\n"
         "- A refusal comes with the option that would allow it.\n\n"
         "After a failed download, look here first, then widen only what the task needs: each suggested option "
         "opens the sandbox further. Texts from the sandbox (hosts, URLs) are shown escaped. Activity's Show in "
         "Connections opens a network event here, filtered to its host.",
         "Verbindungen zeigt das Protokoll des Verbindungs-Proxys eines Projekts und Laufs: jede Anfrage, jeden Push und "
         "jede Abweisung. Wählen Sie das Protokollverzeichnis des Projekts, an dem Sie arbeiten; das Protokoll läuft mit, "
         "solange die Sandbox läuft.\n\n"
         "- Filtern Sie nach Host, Pfad oder Methode. nur Abgelehntes und Fehler zeigt, was nicht durchgelassen "
         "wurde, in Rot.\n"
         "- Wählen Sie eine Zeile, um ihren ganzen Eintrag zu sehen: Methode, URL, Header und Bodies "
         "(Anmeldedaten geschwärzt), die Regel, die entschied, und die Antwort.\n"
         "- Eine Abweisung kommt mit der Option, die sie erlauben würde.\n\n"
         "Sehen Sie nach einem fehlgeschlagenen Download zuerst hier nach und erweitern Sie dann nur, was die "
         "Aufgabe braucht: Jede vorgeschlagene Option öffnet die Sandbox weiter. Texte aus der Sandbox (Hosts, "
         "URLs) werden maskiert gezeigt. In Verbindungen zeigen auf der Seite Aktivität öffnet ein "
         "Netzwerkereignis hier, gefiltert auf seinen Host."),
        cli=("sandbox-ai logs", "--log-dir", "--log-debug"),
        see=("proxy", "network", "logs", "page-activity"),
        tours=("approvals-network", "area:connections")),
    section(
        "page-activity", "app", ("Activity", "Aktivität"),
        ("Activity shows what the agents did in a run, a session or every run of a project, in order and live: "
         "prompts, replies, tool calls, shell commands with exit code and duration, file changes, network events "
         "allowed and refused, approvals, MCP tool calls, the session's start, pause, resume and end, the taint, "
         "and the tokens of each turn. Subagents appear under their own name.\n\n"
         "- Pick the run to follow; with follow on, new events appear as they happen.\n"
         "- Filter by kind and by text, for example only commands and file changes. Refusals and errors are "
         "red.\n"
         "- Select an event to see its full record. Show in Connections opens a network event's log filtered "
         "to its host; Show in Files (or a double click) opens a file event's folder.\n"
         "- Export… writes what the filters show as JSON lines or text.\n"
         "- The Canaries tab lists every opened decoy file and every canary token that left, in red: when, which "
         "canary and path, the process found holding the file (or unknown, with why), and, unfolded, the agent's "
         "tool call and prompt turn at that moment and the model requests around it (`sandbox-ai canaries "
         "log`).\n\n"
         "Everything here may quote the agent, so it is shown as plain text with control characters escaped. "
         "The timeline needs a log directory, which every run with the connection proxy has.",
         "Aktivität zeigt, was die Agenten in einem Lauf, einer Sitzung oder allen Läufen eines Projekts taten, "
         "der Reihe nach und live: Prompts, Antworten, Werkzeugaufrufe, Shell-Befehle mit Exit-Code und Dauer, "
         "Dateiänderungen, erlaubte und abgewiesene Netzwerkereignisse, Freigaben, MCP-Aufrufe, Start, Pause, "
         "Fortsetzen und Ende der Sitzung, den Taint und die Tokens jeder Runde. Subagenten erscheinen unter "
         "eigenem Namen.\n\n"
         "- Wählen Sie den Lauf; mit mitlaufen erscheinen neue Ereignisse, sobald sie geschehen.\n"
         "- Filtern Sie nach Art und Text, etwa nur Befehle und Dateiänderungen. Abweisungen und Fehler sind "
         "rot.\n"
         "- Wählen Sie ein Ereignis, um seinen ganzen Eintrag zu sehen. In Verbindungen zeigen öffnet das Protokoll "
         "eines Netzwerkereignisses, gefiltert auf seinen Host; In Dateien zeigen (oder ein Doppelklick) öffnet "
         "den Ordner eines Dateiereignisses.\n"
         "- Exportieren… schreibt, was die Filter zeigen, als JSON-Zeilen oder Text.\n"
         "- Der Reiter Canaries listet jede geöffnete Köderdatei und jedes Canary-Token, das hinausging, in Rot: "
         "wann, welcher Canary und Pfad, der Prozess, der die Datei offen hielt (oder unbekannt, mit Grund), und "
         "aufgeklappt den Werkzeugaufruf und die Prompt-Runde des Agenten in diesem Moment und die Anfragen an "
         "das Modell darum herum (`sandbox-ai canaries log`).\n\n"
         "Alles hier kann den Agenten zitieren und wird daher als reiner Text mit maskierten Steuerzeichen "
         "gezeigt. Die Zeitleiste braucht ein Protokollverzeichnis; jeder Lauf mit Verbindungs-Proxy hat eines."),
        cli=("sandbox-ai activity --follow", "sandbox-ai activity --project PATH", "sandbox-ai activity --json",
             "sandbox-ai canaries log [RUN] [--json]"),
        see=("logs", "taint", "page-connections", "page-files"),
        tours=("area:activity",)),
    section(
        "page-statistics", "app", ("Statistics", "Statistik"),
        ("Statistics sums up a project's log directory: model calls and tokens per run and per agent, HTTP "
         "requests per host, credential headers sent per host (names only, never values), pushes checked and "
         "refused, refused connections, programs started and the MCP servers' tool calls. The figures are "
         "tables with simple bars.\n\n"
         "Pick a project's log directory, then compare two runs, for example before and after a change of "
         "options. A host you do not expect, or a credential header sent to it, is a reason to look at "
         "Connections. The tokens come from what the model APIs reported in their answers.",
         "Statistik fasst das Protokollverzeichnis eines Projekts zusammen: Modellaufrufe und Tokens pro Lauf und pro "
         "Agent, HTTP-Anfragen pro Host, gesendete Anmelde-Header pro Host (nur Namen, nie Werte), geprüfte und "
         "abgewiesene Pushes, abgewiesene Verbindungen, gestartete Programme und die Werkzeugaufrufe der "
         "MCP-Server. Die Zahlen stehen in Tabellen mit einfachen Balken.\n\n"
         "Wählen Sie das Protokollverzeichnis eines Projekts und vergleichen Sie zwei Läufe, etwa vor und nach einer "
         "Änderung der Optionen. Ein unerwarteter Host oder ein an ihn gesendeter Anmelde-Header ist ein Grund, "
         "auf Verbindungen nachzusehen. Die Tokens stammen aus dem, was die Modell-APIs in ihren Antworten "
         "meldeten."),
        cli=("sandbox-ai logs", "sandbox-ai logs --project PATH"),
        see=("logs", "page-connections"),
        tours=("area:statistics",)),
    section(
        "page-files", "app", ("Files", "Dateien"),
        ("Files lets you look into the workspaces of every running sandbox and session, their HOME when it is on "
         "the host (a session's, or a run's `--state-dir`), the current project and any folder you choose, "
         "without opening a shell in the sandbox.\n\n"
         "- Two panes, as in a two-pane file manager: a sandbox's folder on the left, this machine on the right. "
         "The left pane's chooser lists the workspaces and HOME of each sandbox and session (Refresh looks for "
         "them again, Other folder… adds a folder of your own); the right pane starts in your HOME (Home goes "
         "back there). Each pane has Up (Backspace), a path field and Refresh (Ctrl+R).\n"
         "- Copy (F5) and Move (F6) take what is selected in the active pane into the other pane's folder, without "
         "giving the sandbox a mount. New folder (F7), Delete (F8), Rename (F2), Permissions… and Owner… work in "
         "the active pane; Take ownership… takes back what a rootless Podman sandbox wrote as another user. Tab "
         "switches panes; a click makes a pane the active one.\n"
         "- Replace existing files allows overwriting a file of the same name; off, nothing is ever replaced. "
         "Delete and a replacing copy or move ask first.\n\n"
         "Symbolic links are shown with their target and never followed, neither to list nor to copy. Copies "
         "stop at 1 GiB, run in the background and can be cancelled; a file gets its name only when it is "
         "complete, so nothing truncated is left behind. Files copied to the host lose their executable bits. "
         "Files that tools run or read as configuration (`.git/config`, hooks, `.envrc`) are listed with a "
         "warning after the copy: read them before you use git there.",
         "Dateien lässt Sie in die Arbeitsordner jeder laufenden Sandbox und Sitzung sehen, in ihr HOME, wenn es auf "
         "dem Host liegt (das einer Sitzung oder das `--state-dir` eines Laufs), in das aktuelle Projekt und in "
         "jeden gewählten Ordner, ohne eine Shell in der Sandbox zu öffnen.\n\n"
         "- Zwei Bereiche wie in einem zweispaltigen Dateimanager: links der Ordner einer Sandbox, rechts dieser "
         "Rechner. Die Auswahl des linken Bereichs listet die Arbeitsordner und das HOME jeder Sandbox und Sitzung "
         "(Neu laden sucht sie erneut, Anderer Ordner… fügt einen eigenen Ordner hinzu); der rechte Bereich "
         "beginnt in Ihrem HOME (Persönlicher Ordner führt dorthin zurück). Jeder Bereich hat Nach oben "
         "(Rücktaste), ein Pfadfeld und Neu laden (Strg+R).\n"
         "- Kopieren (F5) und Verschieben (F6) bringen das im aktiven Bereich Gewählte in den Ordner des anderen "
         "Bereichs, ohne der Sandbox ein Mount zu geben. Neuer Ordner (F7), Löschen (F8), Umbenennen (F2), "
         "Berechtigungen … und Besitzer … wirken im aktiven Bereich; Besitz übernehmen … holt zurück, was eine "
         "rootless-Podman-Sandbox als anderer Benutzer geschrieben hat. Tab wechselt den Bereich; ein Klick macht "
         "einen Bereich zum aktiven.\n"
         "- Vorhandene Dateien ersetzen erlaubt das Überschreiben gleichnamiger Dateien; aus, wird nie etwas "
         "ersetzt. Löschen und ein ersetzendes Kopieren oder Verschieben fragen zuerst.\n\n"
         "Symbolische Links werden mit ihrem Ziel gezeigt und nie verfolgt, weder beim Auflisten noch beim "
         "Kopieren. Kopien enden bei 1 GiB, laufen im Hintergrund und lassen sich abbrechen; eine Datei bekommt "
         "ihren Namen erst, wenn sie vollständig ist, so bleibt nichts Abgeschnittenes zurück. Auf den Host "
         "kopierte Dateien verlieren ihre Ausführungsrechte. Dateien, die Werkzeuge ausführen oder als "
         "Konfiguration lesen (`.git/config`, Hooks, `.envrc`), werden nach dem Kopieren mit einer Warnung "
         "gelistet: Lesen Sie sie, bevor Sie dort git benutzen."),
        cli=("--state-dir",),
        see=("workspaces-mounts", "sandbox", "page-activity"),
        tours=("area:files",)),
    section(
        "page-kits", "app", ("Kits & plugins", "Kits & Plugins"),
        ("Kits & plugins shows every kit: sandbox-ai's own kit and the default kit (both in every sandbox), "
         "built-in kits such as `egm` and yours. For each agent it lists what a kit brings (skills, agents, "
         "commands, prompts, the memory template, marketplaces and plugins) and whether the current project uses "
         "it. The tab Marketplaces is the marketplace browser of the host chosen (`sandbox-ai plugins "
         "marketplaces`): choose a kit (the default kit or one of yours; the built-in ones are read-only) and a "
         "marketplace, and its plugins show with their state (in which kits, on or off, installed in the plugin "
         "store, installed or enabled in the agent's own configuration), with a filter. Add to the kit, Enable, "
         "Disable and Remove change that kit; a marketplace is added, imported from Claude (with the plugins Claude "
         "enables) or from Codex (for Claude), or removed (with its plugins, if you tick that). OpenCode's npm "
         "plugins are listed and switched the same way. Fetch the plugin lists runs `sandbox-ai plugins claude "
         "--marketplaces-only` in a terminal: it installs nothing. Every change is asked first, naming the host and "
         "the kit; the agents' own configuration is only read.\n\n"
         "- New kit…, Add skill from a folder…, New skill…, Add plugin…, Add marketplace…, Add hook…, Remove… "
         "and Delete kit… run `sandbox-ai kits`. sandbox-ai's own and the built-in kits are read-only: with one "
         "of them selected, what you add goes to a kit of yours, which you choose (or name, and it is created) "
         "first.\n"
         "- Use in the current project… adds `--kit NAME` to the project's `.sandbox-ai.json` through the "
         "Board's Apply (shown, then written and trusted).\n"
         "- Install plugins and Update plugins run `sandbox-ai plugins claude` in a terminal.\n"
         "- Review and approve hooks… runs `sandbox-ai kits trust-hooks KIT` in a terminal, where you read "
         "every file and confirm. The app never approves on its own.\n\n"
         "A hook runs code at the agent's events, and a plugin runs code in every sandbox that enables it: "
         "approve and install only what you trust.",
         "Kits & Plugins zeigt jedes Kit: das eigene Kit von sandbox-ai und das Standard-Kit (beide in jeder "
         "Sandbox), eingebaute Kits wie `egm` und Ihre eigenen. Je Agent steht da, was ein Kit mitbringt (Skills, "
         "Agenten, Befehle, Prompts, die Memory-Vorlage, Marktplätze und Plugins) und ob das aktuelle Projekt es "
         "nutzt. Der Reiter Marktplätze ist der Marktplatz-Browser des gewählten Hosts (`sandbox-ai plugins "
         "marketplaces`): Wählen Sie ein Kit (das Standard-Kit oder eines Ihrer Kits; die eingebauten sind "
         "schreibgeschützt) und einen Marktplatz, dann zeigen sich seine Plugins mit ihrem Zustand (in welchen Kits, "
         "an oder aus, im Plugin-Speicher installiert, in der eigenen Konfiguration des Agenten installiert oder "
         "eingeschaltet), mit einem Filter. Zum Kit hinzufügen, Einschalten, Ausschalten und Entfernen ändern dieses "
         "Kit; ein Marktplatz wird hinzugefügt, von Claude (mit den Plugins, die Claude einschaltet) oder von Codex "
         "(für Claude) importiert oder entfernt (mit seinen Plugins, wenn Sie das ankreuzen). Die npm-Plugins von "
         "OpenCode werden genauso gelistet und geschaltet. Plugin-Listen holen führt `sandbox-ai plugins claude "
         "--marketplaces-only` in einem Terminal aus: Es installiert nichts. Jede Änderung wird zuerst erfragt, mit "
         "Host und Kit; die eigene Konfiguration der Agenten wird nur gelesen.\n\n"
         "- Neues Kit…, Skill aus einem Ordner…, Neuer Skill…, Plugin hinzufügen…, Marktplatz hinzufügen…, Hook "
         "hinzufügen…, Entfernen… und Kit löschen… führen `sandbox-ai kits` aus. Das eigene Kit von sandbox-ai und "
         "die eingebauten Kits sind schreibgeschützt: Ist eines davon ausgewählt, kommt, was Sie hinzufügen, in "
         "ein eigenes Kit, das Sie zuerst wählen (oder benennen, dann wird es angelegt).\n"
         "- Im aktuellen Projekt nutzen… trägt `--kit NAME` über Übernehmen der Tafel in die `.sandbox-ai.json` "
         "des Projekts ein (gezeigt, dann geschrieben und vertraut).\n"
         "- Plugins installieren und Plugins aktualisieren führen `sandbox-ai plugins claude` in einem Terminal "
         "aus.\n"
         "- Hooks prüfen und freigeben… startet `sandbox-ai kits trust-hooks KIT` in einem Terminal, wo Sie jede "
         "Datei lesen und bestätigen. Die App gibt nie selbst frei.\n\n"
         "Ein Hook führt Code bei Ereignissen des Agenten aus, ein Plugin in jeder Sandbox, die es aktiviert: "
         "Geben Sie nur frei und installieren Sie nur, was Sie für vertrauenswürdig halten."),
        cli=("sandbox-ai kits", "sandbox-ai kits show NAME", "sandbox-ai kits trust-hooks KIT",
             "sandbox-ai plugins claude", "sandbox-ai plugins --status", "sandbox-ai plugins marketplaces",
             "sandbox-ai plugins claude --marketplaces-only", "--kit", "--no-default-kit"),
        see=("kits", "page-board"),
        tours=("area:kits",)),
    section(
        "page-mcp", "app", ("MCP servers", "MCP-Server"),
        ("MCP servers lists your servers with their status, and the catalogs of sandbox-ai-mcp (`mcp:`) and "
         "axross-mcp-hub (`hub:`). Each server runs in a sandbox or container of its own, with only the network "
         "and folders its definition names; agents reach it through a relay that applies your rules.\n\n"
         "- Select a server to see its definition; start or stop it.\n"
         "- Catalog servers are audited and pinned by digest. Add… asks for folders, variables, network and "
         "limits and runs `sandbox-ai mcp add`; Reload catalogs reads them again.\n"
         "- The Catalog column names the catalog a server was added from or that lists an entry, and whether its "
         "signature was verified. An entry whose catalog no trusted key signed is added only when you say so: "
         "refused for that, the page asks whether to add it anyway (`--unsigned`). Nobody vouched for how such a "
         "server was built; it runs in a sandbox of its own all the same.\n"
         "- A start uses a server with `--mcp NAME`, or connect it to the agent on the Board.\n\n"
         "Give a server network only where you would give it to the agent: a fetch or browser server sends "
         "whatever the agent asks it to, past the agent's firewall and approvals. A variable's value comes from "
         "your environment when the server starts and is never stored.",
         "MCP-Server listet Ihre Server mit ihrem Zustand und die Kataloge von sandbox-ai-mcp (`mcp:`) und "
         "axross-mcp-hub (`hub:`). Jeder Server läuft in einer eigenen Sandbox oder einem eigenen Container, nur "
         "mit dem Netzwerk und den Ordnern, die seine Definition nennt; Agenten erreichen ihn über ein Relay, das "
         "Ihre Regeln anwendet.\n\n"
         "- Wählen Sie einen Server, um seine Definition zu sehen; starten oder stoppen Sie ihn.\n"
         "- Katalogserver sind geprüft und per Digest festgelegt. Hinzufügen… fragt nach Ordnern, Variablen, "
         "Netzwerk und Limits und führt `sandbox-ai mcp add` aus; Kataloge neu laden liest sie neu.\n"
         "- Die Spalte Katalog nennt den Katalog, aus dem ein Server hinzugefügt wurde oder der einen Eintrag "
         "listet, und ob seine Signatur geprüft wurde. Einen Eintrag, dessen Katalog kein vertrauenswürdiger "
         "Schlüssel signiert hat, fügt die App nur hinzu, wenn Sie es sagen: Wird er deshalb abgelehnt, fragt die "
         "Seite, ob er trotzdem hinzugefügt werden soll (`--unsigned`). Niemand hat für den Bau eines solchen "
         "Servers gebürgt; er läuft trotzdem in einer eigenen Sandbox.\n"
         "- Ein Start nutzt einen Server mit `--mcp NAME`, oder Sie verbinden ihn auf der Tafel mit dem "
         "Agenten.\n\n"
         "Geben Sie einem Server nur Netzwerk, wo Sie es auch dem Agenten geben würden: Ein Fetch- oder "
         "Browser-Server schickt, was der Agent verlangt, an dessen Firewall und Freigaben vorbei. Der Wert einer "
         "Variablen kommt beim Start des Servers aus Ihrer Umgebung und wird nie gespeichert."),
        cli=("sandbox-ai mcp list", "sandbox-ai mcp catalog", "sandbox-ai mcp add NAME", "sandbox-ai mcp start NAME",
             "sandbox-ai mcp stop NAME", "--mcp"),
        see=("mcp", "rulesets", "vpn-tor"),
        tours=("area:mcp",)),
    section(
        "page-llm", "app", ("LLM servers", "LLM-Server"),
        ("LLM servers lists the model servers `sandbox-ai llm` keeps by name, so that an agent can work with a "
         "model of your own instead of the cloud: Ollama, LM Studio, the llama.cpp server, vLLM or another "
         "OpenAI-compatible server, on this machine, in your LAN or reached through a VPN. Host chooses whose "
         "list it is: this machine's or a host's of `sandbox-ai hosts` (over SSH). The page asks only while it "
         "is shown or a button asks.\n\n"
         "- The table shows each server's name, kind, URL, default model, key, VPN profile and the agents it "
         "suits. Below it, a line per server says what it has loaded now and what it uses (`sandbox-ai llm status`, "
         "read after the list and every 20 seconds while the page is shown, in the background): the loaded models "
         "with the memory they take or their size, VRAM and RAM, the KV cache, the requests running and waiting - "
         "as far as the server's API reports it. Ollama reports the loaded models with VRAM and RAM, vLLM the KV "
         "cache and the requests, llama.cpp its slots (the requests with `--metrics`), LM Studio the loaded "
         "models; what a server does not report is said, never guessed. The host's total RAM and VRAM are not "
         "reported by these APIs. A host whose sandbox-ai is older says to update it.\n"
         "- Add… registers one: name, kind, URL (the server's root), optionally a model, a VPN profile, whether it "
         "also speaks the Anthropic Messages API (for Claude Code) and a key. `sandbox-ai llm add` checks the server "
         "first; only then is a key typed in for this machine kept as a secret of sandbox-ai (on standard input, "
         "never on a command line), and the server only names that secret. When a secret of that name is kept "
         "already, you are asked before its value is replaced. On a host, name a secret kept there already "
         "(Credentials › Secrets keeps one there).\n"
         "- Test connection in the dialog lists the models (with their sizes, loaded first) and says what the "
         "server has loaded and uses now. Every model is offered as the default model: type to filter a long "
         "list.\n"
         "- Check tests reachability, TLS, the key, the model and the API the chosen agent needs (or every "
         "agent's). Models lists what the server offers now, with Size and Loaded where the server reports them. "
         "Remove… forgets a server after asking; its secret stays.\n\n"
         "A start uses a server with `--llm NAME` or `--llm NAME:MODEL`, or connect an LLM part to the agent on "
         "the Board: only the server's address and port are opened, and the connection proxy adds the key on the "
         "wire, so the agent only sees a placeholder. Later, while you work: select such a start on Sandboxes & "
         "sessions, and the line below the table shows what its server has loaded and uses now, read again every "
         "20 seconds.",
         "LLM-Server listet die Modellserver, die `sandbox-ai llm` unter einem Namen aufbewahrt, damit ein Agent "
         "mit einem eigenen Modell statt der Cloud arbeiten kann: Ollama, LM Studio, der Server von llama.cpp, "
         "vLLM oder ein anderer OpenAI-kompatibler Server, auf diesem Rechner, im LAN oder über ein VPN erreicht. "
         "Host wählt, wessen Liste es ist: die dieses Rechners oder die eines Hosts von `sandbox-ai hosts` (über "
         "SSH). Die Seite fragt nur, solange sie zu sehen ist oder ein Knopf es verlangt.\n\n"
         "- Die Tabelle zeigt Name, Art, URL, Standardmodell, Schlüssel und VPN-Profil jedes Servers und die "
         "Agenten, zu denen er passt. Darunter sagt eine Zeile pro Server, was er jetzt geladen hat und belegt "
         "(`sandbox-ai llm status`, gelesen nach der Liste und alle 20 Sekunden, solange die Seite zu sehen ist, im "
         "Hintergrund): die geladenen Modelle mit dem Speicher, den sie belegen, oder ihrer Größe, VRAM und RAM, "
         "den KV-Cache, die laufenden und wartenden Anfragen - soweit die API des Servers es meldet. Ollama meldet "
         "die geladenen Modelle mit VRAM und RAM, vLLM den KV-Cache und die Anfragen, llama.cpp seine Slots (die "
         "Anfragen mit `--metrics`), LM Studio die geladenen Modelle; was ein Server nicht meldet, wird gesagt, nie "
         "geschätzt. Den gesamten RAM und VRAM des Hosts melden diese APIs nicht. Ein Host mit älterem sandbox-ai "
         "sagt, dass er aktualisiert werden muss.\n"
         "- Hinzufügen… trägt einen ein: Name, Art, URL (die Wurzel des Servers), optional ein Modell, ein "
         "VPN-Profil, ob er auch die Anthropic-Messages-API spricht (für Claude Code) und einen Schlüssel. Ein für "
         "diesen Rechner eingegebener Schlüssel wird erst als Geheimnis von sandbox-ai aufbewahrt, wenn `sandbox-ai "
         "llm add` den Server geprüft und angenommen hat (über die Standardeingabe, nie auf einer Befehlszeile), "
         "und der Server nennt nur dieses Geheimnis. Gibt es schon ein Geheimnis dieses Namens, werden Sie gefragt, "
         "bevor sein Wert ersetzt wird. Auf einem Host nennen Sie ein dort schon aufbewahrtes Geheimnis "
         "(Zugangsdaten › Geheimnisse legt dort eines ab).\n"
         "- Verbindung testen im Dialog listet die Modelle (mit ihrer Größe, geladene zuerst) und sagt, was der "
         "Server jetzt geladen hat und belegt. Jedes Modell wird als Standardmodell angeboten: Tippen filtert eine "
         "lange Liste.\n"
         "- Prüfen testet Erreichbarkeit, TLS, den Schlüssel, das Modell und die API, die der gewählte Agent "
         "braucht (oder die jedes Agenten). Modelle listet, was der Server gerade anbietet, mit Größe und Geladen, "
         "wo der Server sie meldet. Entfernen… vergisst einen Server nach Rückfrage; sein Geheimnis bleibt.\n\n"
         "Ein Start nutzt einen Server mit `--llm NAME` oder `--llm NAME:MODELL`, oder Sie verbinden auf der Tafel "
         "ein LLM-Teil mit dem Agenten: Nur Adresse und Port des Servers werden geöffnet, und der Verbindungs-Proxy "
         "fügt den Schlüssel unterwegs hinzu, sodass der Agent nur einen Platzhalter sieht. Später, während Sie "
         "arbeiten: Wählen Sie einen solchen Start unter Sandboxen & Sitzungen, und die Zeile unter der Tabelle "
         "zeigt, was sein Server jetzt geladen hat und belegt, alle 20 Sekunden neu gelesen."),
        cli=("sandbox-ai llm list", "sandbox-ai llm add NAME --kind KIND --url URL", "sandbox-ai llm check NAME",
             "sandbox-ai llm models NAME", "sandbox-ai llm status [NAME]", "sandbox-ai llm remove NAME",
             "--llm NAME[:MODEL]"),
        see=("llm", "secrets", "vpn-tor", "page-hosts", "page-board"),
        tours=("area:llm",)),
    section(
        "page-images", "app", ("Images", "Images"),
        ("Images lists each agent's image and your variants: built or not, whether each is current with its "
         "extension (or needs a rebuild for a changed extension or a new base), agent version, created and size, "
         "with the installed packages of the selected image and a filter.\n\n"
         "- Build runs `sandbox-ai build` for the selected image; Other builds offers the app's terminal, a "
         "terminal window, only the extension layer, or all images.\n"
         "- New variant… makes a copy of an agent's image with its own extension (from the agent's extension or "
         "empty); start it with `--image-variant`. Delete variant… asks first.\n"
         "- The extension editor has tabs for Debian packages, npm packages, Python tools, files copied in, and a "
         "Containerfile fragment with syntax highlighting and the validator's findings in place; a preview shows "
         "the final Containerfile.\n\n"
         "Save runs `sandbox-ai images extend` and is refused while there are errors; the app never writes an "
         "extension itself. Saving changes the extension only: the image changes when you build it again.",
         "Images listet das Image jedes Agenten und Ihre Varianten: gebaut oder nicht, ob jedes zu seiner "
         "Erweiterung passt (oder für eine geänderte Erweiterung oder eine neue Basis neu gebaut werden muss), "
         "Agent-Version, Erstellung und Größe, dazu die installierten Pakete des gewählten Images mit Filter.\n\n"
         "- Bauen führt `sandbox-ai build` für das gewählte Image aus; Weitere Builds bietet das Terminal der App, "
         "ein Terminalfenster, nur die Erweiterungsschicht oder alle Images.\n"
         "- Neue Variante… legt eine Kopie des Images eines Agenten mit eigener Erweiterung an (aus der "
         "Erweiterung des Agenten oder leer); starten Sie sie mit `--image-variant`. Variante löschen… fragt "
         "zuerst.\n"
         "- Der Erweiterungs-Editor hat Tabs für Debian-Pakete, npm-Pakete, Python-Werkzeuge, hineinkopierte "
         "Dateien und ein Containerfile-Fragment mit Syntaxhervorhebung und den Befunden der Prüfung im Text; eine "
         "Vorschau zeigt das fertige Containerfile.\n\n"
         "Speichern führt `sandbox-ai images extend` aus und wird verweigert, solange es Fehler gibt; die App "
         "schreibt nie selbst eine Erweiterung. Speichern ändert nur die Erweiterung: Das Image ändert sich, wenn "
         "Sie es neu bauen."),
        cli=("sandbox-ai images", "sandbox-ai build AGENT|all|VARIANT", "sandbox-ai images new",
             "sandbox-ai images extend", "sandbox-ai images remove", "--image-variant"),
        see=("sandbox", "releases-update", "page-system"),
        tours=("area:images",)),
    section(
        "page-team", "app", ("Teams", "Teams"),
        ("Teams lists the teams of `sandbox-ai team`: two or three agents, one sandbox each, on one shared "
         "workspace, started and stopped together. The first agent leads: you talk to it, and it hands the others "
         "tasks on the team's board, an MCP server of the team in a sandbox without network. Host chooses whose "
         "teams these are: this machine's or a host's of `sandbox-ai hosts` (over SSH).\n\n"
         "- Start a team… asks for a name, the lead, the members, the workspace (a path on that host) and launcher "
         "options for every member, then runs `sandbox-ai team start` in a terminal tab: each member starts there, "
         "one after the other.\n"
         "- Select a team to see its members (each a session NAME-AGENT, with its state) and its board.\n"
         "- Talk to the lead attaches to the lead's session in a terminal tab. Post a task… puts a task for one "
         "member on the board, from you.\n"
         "- Stop… ends every member and the board; Delete… also removes the members' HOMEs and the board, for "
         "good. Both ask first; the workspace stays.",
         "Teams listet die Teams von `sandbox-ai team`: zwei oder drei Agenten, je eine Sandbox, auf einem "
         "gemeinsamen Arbeitsordner, zusammen gestartet und gestoppt. Der erste Agent leitet: Mit ihm sprechen Sie, "
         "und er gibt den anderen Aufgaben auf der Tafel des Teams, einem MCP-Server des Teams in einer Sandbox ohne "
         "Netzwerk. Host wählt, wessen Teams es sind: die dieses Rechners oder die eines Hosts von `sandbox-ai "
         "hosts` (über SSH).\n\n"
         "- Team starten… fragt nach Name, Leitung, Mitgliedern, Arbeitsordner (ein Pfad auf jenem Host) und "
         "Startoptionen für jedes Mitglied und führt dann `sandbox-ai team start` in einem Terminal-Tab aus: Jedes "
         "Mitglied startet dort, eines nach dem anderen.\n"
         "- Wählen Sie ein Team, um seine Mitglieder (je eine Sitzung NAME-AGENT, mit ihrem Stand) und seine Tafel "
         "zu sehen.\n"
         "- Mit der Leitung sprechen verbindet ein Terminal-Tab mit der Sitzung der Leitung. Aufgabe stellen… legt "
         "eine Aufgabe für ein Mitglied auf die Tafel, von Ihnen.\n"
         "- Stoppen… beendet jedes Mitglied und die Tafel; Löschen… entfernt auch die HOMEs der Mitglieder und die "
         "Tafel, endgültig. Beides fragt zuerst; der Arbeitsordner bleibt."),
        cli=("sandbox-ai team start NAME --agents claude,codex -w DIR", "sandbox-ai team list",
             "sandbox-ai team show NAME", "sandbox-ai team post NAME AGENT TASK", "sandbox-ai team stop NAME",
             "sandbox-ai team delete NAME --yes"),
        see=("teams", "sessions", "page-sandboxes", "page-hosts"),
        tours=("area:team",)),
    section(
        "page-credentials", "app", ("Credentials", "Zugangsdaten"),
        ("Credentials keeps what sandboxes may log in with. A value kept is never read back or shown.\n\n"
         "- Agent logins for sandboxes: each agent's login, from sandbox-ai's login store or the host, valid or "
         "expired. Log in… and Log out… run in a terminal. A sandbox gets the login when it starts.\n"
         "- Secrets: API keys and tokens by name, kept for good in your keyring or private files. Add keeps the "
         "value typed under its name and passes it on standard input, never as an argument; a name kept already is "
         "replaced only after a yes. Remove… asks first. Every field where a value is typed (a secret, an LLM "
         "server's key, a VPN key, a typed Git token) has Show value beside it: on by default, so you see what you "
         "type; turned off, the field shows dots, and the choice is kept for every such field.\n"
         "- VPN profiles: Add… for WireGuard, OpenVPN, Mullvad or Tailscale/Headscale, Check, Remove…. Keys and "
         "passwords become secrets; the profile only names them.\n"
         "- Identities: who a sandbox works as (commit name and address, Git and publish logins by secret "
         "name), added and edited through `sandbox-ai identities`.\n\n"
         "A start names the secrets it needs. With `--api-key`, `--publish`, Git logins or `--token-proxy` the "
         "token never enters the sandbox; with `--secret-env` the agent can read it.",
         "Zugangsdaten verwaltet, womit sich Sandboxen anmelden dürfen. Ein gespeicherter Wert wird nie "
         "zurückgelesen oder gezeigt.\n\n"
         "- Agent-Anmeldungen für Sandboxen: der Login jedes Agenten, aus dem Login-Speicher von sandbox-ai oder "
         "vom Host, gültig oder abgelaufen. Anmelden… und Abmelden… laufen in einem Terminal. Eine Sandbox bekommt "
         "den Login beim Start.\n"
         "- Geheimnisse: API-Schlüssel und Tokens unter Namen, dauerhaft in Ihrem Schlüsselbund oder privaten "
         "Dateien. Hinzufügen speichert den eingegebenen Wert unter seinem Namen und übergibt ihn über die "
         "Standardeingabe, nie als Argument; ein schon gespeicherter Name wird erst nach einem Ja ersetzt. "
         "Entfernen… fragt zuerst. Jedes Feld, in das ein Wert eingegeben wird (ein Geheimnis, der Schlüssel eines "
         "LLM-Servers, ein VPN-Schlüssel, ein eingegebenes Git-Token), hat daneben Wert anzeigen: standardmäßig an, "
         "damit Sie sehen, was Sie eingeben; ausgeschaltet zeigt das Feld Punkte, und die Wahl gilt für jedes "
         "solche Feld.\n"
         "- VPN-Profile: Hinzufügen… für WireGuard, OpenVPN, Mullvad oder Tailscale/Headscale, Prüfen, "
         "Entfernen…. Schlüssel und Passwörter werden zu Geheimnissen; das Profil nennt sie nur.\n"
         "- Identitäten: als wer eine Sandbox arbeitet (Commit-Name und Adresse, Git- und Publish-Logins über den "
         "Namen eines Geheimnisses), angelegt und bearbeitet über `sandbox-ai identities`.\n\n"
         "Ein Start nennt die Geheimnisse, die er braucht. Mit `--api-key`, `--publish`, Git-Logins oder "
         "`--token-proxy` kommt das Token nie in die Sandbox; mit `--secret-env` kann der Agent es lesen."),
        cli=("sandbox-ai login --status", "sandbox-ai login AGENT", "sandbox-ai secrets list",
             "sandbox-ai secrets set NAME", "sandbox-ai vpn list", "sandbox-ai identities list"),
        see=("secrets", "vpn-tor", "proxy"),
        tours=("area:credentials",)),
    section(
        "page-system", "app", ("System", "System"),
        ("System shows the sandbox-ai in use and keeps the host in order.\n\n"
         "- sandbox-ai: the version and its folder. Check for updates runs `sandbox-ai update --check` in the "
         "background.\n"
         "- Administrator policy: one line on `/etc/sandbox-ai/policy.json`; Show… opens it in full, All "
         "settings… opens the Settings dialog.\n"
         "- Host check (sandbox-ai doctor): choose the backend (podman, bwrap or both) and press Check. Each row "
         "says what is wrong and how to fix it.\n"
         "- Images, updates, privacy: Build images, Update sandbox-ai, Privacy check of a repository….\n"
         "- Settings of the app: theme, language, where commands such as builds and logins run, whether closing "
         "the window keeps the app answering in the tray, and a start at login in the tray.\n\n"
         "Run the host check after installing and fix the first red row first. A start at login is refused when "
         "the app or its sandbox-ai lies in a folder a sandbox can write.",
         "System zeigt das verwendete sandbox-ai und hält den Host in Ordnung.\n\n"
         "- sandbox-ai: Version und Ordner. Auf Updates prüfen führt `sandbox-ai update --check` im Hintergrund "
         "aus.\n"
         "- Richtlinie des Administrators: eine Zeile zu `/etc/sandbox-ai/policy.json`; Anzeigen… zeigt sie ganz, "
         "Alle Einstellungen… öffnet den Dialog Einstellungen.\n"
         "- Host-Prüfung (sandbox-ai doctor): Wählen Sie das Backend (podman, bwrap oder both) und drücken Sie "
         "Prüfen. Jede Zeile sagt, was fehlt und wie Sie es beheben.\n"
         "- Images, Updates, Datenschutz: Images bauen, sandbox-ai aktualisieren, Datenschutzprüfung eines "
         "Repositorys….\n"
         "- Einstellungen der App: Farbschema, Sprache, wo Befehle wie Builds und Anmeldungen laufen, ob die App "
         "nach dem Schließen des Fensters im Infobereich weiter antwortet, und ein Start bei der Anmeldung im "
         "Infobereich.\n\n"
         "Führen Sie die Host-Prüfung nach der Installation aus und beheben Sie zuerst die erste rote Zeile. Ein "
         "Start bei der Anmeldung wird verweigert, wenn die App oder ihr sandbox-ai in einem Ordner liegt, den "
         "eine Sandbox schreiben kann."),
        cli=("sandbox-ai doctor", "sandbox-ai update --check", "sandbox-ai update", "sandbox-ai build all",
             "sandbox-ai privacy-check"),
        see=("app-settings", "policy", "releases-update", "privacy-check"),
        tours=("area:system",)),
    section(
        "page-hosts", "app", ("Hosts", "Hosts"),
        ("Hosts manages other machines, such as Proxmox LXC containers or servers, whose sandbox-ai this app "
         "controls over SSH. The list is the one `sandbox-ai hosts` keeps; the web interface uses it too.\n\n"
         "The table has one row per host: its SSH address, whether it answered (or when it was last seen), its "
         "sandbox-ai version, how many starts run there and how many questions wait. The starts and questions "
         "come from the Sandboxes and Approvals pages, which read every host anyway; Refresh asks every host now.\n\n"
         "- Show starts opens Sandboxes & sessions with only this host's starts, Show questions the Approvals page "
         "with only its questions: attach, pause, stop, allow and deny happen there, as for this computer.\n"
         "- Add… adds a host with a name and its SSH address (user@host). Edit… changes whether its update runs "
         "with sudo (`sandbox-ai hosts set NAME --update-sudo on|off`); a new address or key is Remove… and Add…. "
         "Test runs SSH, the host's sandbox-ai and its doctor.\n"
         "- Update and Update all run each host's own signed `sandbox-ai update`; a sudo that wants a password "
         "opens it in a terminal tab. Check versions asks every host for its version now.\n"
         "- Update from here… puts this machine's sandbox-ai on the chosen host instead (`sandbox-ai hosts update "
         "NAME --from-here`), as the same kind of installation it has there (the user's own or the system's, with "
         "sudo as chosen beside Update): for a host that cannot reach the release server, or before a release is "
         "published. It is asked first, like an install; an update that finds no published release offers it at "
         "once.\n\n"
         "The tabs on the right are about the chosen host: Doctor shows its checks with the commands that fix "
         "them, Updates what the updates printed, and Defaults what every start there uses unless it says "
         "otherwise: the security mode, the canaries, how many days unused kept starts stay (keep_days) and the "
         "agents' telemetry. Save defaults… writes them with `sandbox-ai defaults set` or `unset` there; a step "
         "down in security is asked first.\n\n"
         "Everything runs as `sandbox-ai hosts run HOST -- ARGS`, each argument quoted on its own. SSH uses your "
         "keys and never prompts for a password; the host key is learnt at the first contact and checked "
         "strictly after that. Nothing listens on the hosts but sshd.",
         "Hosts verwaltet andere Rechner, etwa Proxmox-LXC-Container oder Server, deren sandbox-ai diese App über "
         "SSH steuert. Die Liste ist die von `sandbox-ai hosts`; die Weboberfläche nutzt sie auch.\n\n"
         "Die Tabelle hat eine Zeile pro Host: seine SSH-Adresse, ob er antwortete (oder wann er zuletzt gesehen "
         "wurde), seine sandbox-ai-Version, wie viele Starts dort laufen und wie viele Fragen warten. Starts und "
         "Fragen kommen von den Seiten Sandboxen und Freigaben, die ohnehin jeden Host lesen; Neu laden fragt "
         "jetzt jeden Host.\n\n"
         "- Starts zeigen öffnet Sandboxen & Sitzungen nur mit den Starts dieses Hosts, Fragen zeigen die Seite "
         "Freigaben nur mit seinen Fragen: Verbinden, Pausieren, Stoppen, Erlauben und Ablehnen geschehen dort, "
         "wie für diesen Rechner.\n"
         "- Hinzufügen… fügt einen Host mit einem Namen und seiner SSH-Adresse (benutzer@host) hinzu. "
         "Bearbeiten… ändert, ob sein Update mit sudo läuft (`sandbox-ai hosts set NAME --update-sudo on|off`); "
         "eine neue Adresse oder ein neuer Schlüssel ist Entfernen… und Hinzufügen…. Testen prüft SSH, das "
         "sandbox-ai des Hosts und seinen doctor.\n"
         "- Aktualisieren und Alle aktualisieren führen das eigene signierte `sandbox-ai update` jedes Hosts aus; "
         "ein sudo, das ein Passwort will, öffnet es in einem Terminal-Tab. Versionen prüfen fragt jetzt jeden "
         "Host nach seiner Version.\n"
         "- Von hier aktualisieren… bringt stattdessen das sandbox-ai dieses Rechners auf den gewählten Host "
         "(`sandbox-ai hosts update NAME --from-here`), als dieselbe Art Installation, die er dort hat (die des "
         "Benutzers oder die des Systems, mit sudo wie neben Aktualisieren gewählt): für einen Host, der den "
         "Release-Server nicht erreicht, oder bevor ein Release veröffentlicht ist. Es wird vorher gefragt, wie "
         "eine Installation; ein Update, das kein veröffentlichtes Release findet, bietet es gleich an.\n\n"
         "Die Tabs rechts gelten dem gewählten Host: Doctor zeigt seine Prüfungen mit den Befehlen, die sie "
         "beheben, Updates, was die Updates ausgaben, und Standards, was jeder Start dort nutzt, wenn er nichts "
         "anderes sagt: den Sicherheitsmodus, die Canaries, wie viele Tage ungenutzte aufbewahrte Starts bleiben "
         "(keep_days) und die Telemetrie der Agenten. Vorgaben speichern… schreibt sie dort mit `sandbox-ai "
         "defaults set` oder `unset`; ein Schritt zu weniger Sicherheit wird vorher erfragt.\n\n"
         "Alles läuft als `sandbox-ai hosts run HOST -- ARGS`, jedes Argument einzeln gequotet. SSH nutzt Ihre "
         "Schlüssel und fragt nie nach einem Passwort; der Host-Schlüssel wird beim ersten Kontakt gelernt und "
         "danach streng geprüft. Auf den Hosts lauscht nichts außer sshd."),
        cli=("sandbox-ai hosts list", "sandbox-ai hosts add NAME USER@HOST",
             "sandbox-ai hosts set NAME --update-sudo on|off", "sandbox-ai hosts test NAME",
             "sandbox-ai hosts update NAME|--all", "sandbox-ai hosts update NAME --from-here",
             "sandbox-ai hosts run NAME -- ARGS", "sandbox-ai hosts remove NAME",
             "sandbox-ai defaults set | unset"),
        see=("hosts", "web", "page-sandboxes", "page-approvals", "security-modes", "canaries"),
        tours=("area:hosts",)),
    section(
        "page-help", "app", ("Help", "Hilfe"),
        ("The Help page explains every page and feature of the app and the concepts of sandbox-ai.\n\n"
         "- The list on the left holds the sections, grouped into the app and the concepts. Type into the search "
         "field: only sections with every word you type (in English or German) stay.\n"
         "- The text of the selected section is on the right, with the related commands and options as they are "
         "typed, and links to related sections.\n"
         "- A switch shows the text in English or German. It follows the app's language unless you change it.\n"
         "- Buttons start the tours linked to a section: an area tour of a page, or a tour through a task.\n"
         "- Tour about… (also in the Help menu, Ctrl+F1) asks for a topic, such as network, presets or kits, and "
         "lists the tours and sections about it, best first: Enter starts the tour or opens the section.\n\n"
         "Every page, panel and component has a small (?) button. It opens a short explanation that ends with "
         "More in Help, which opens the matching section here.",
         "Die Seite Hilfe erklärt jede Seite und Funktion der App und die Begriffe von sandbox-ai.\n\n"
         "- Die Liste links enthält die Abschnitte, gegliedert in die App und die Begriffe. Tippen Sie in das "
         "Suchfeld: Nur Abschnitte mit jedem getippten Wort (auf Englisch oder Deutsch) bleiben stehen.\n"
         "- Rechts steht der Text des gewählten Abschnitts, mit den zugehörigen Befehlen und Optionen, wie man sie "
         "tippt, und Verweisen auf verwandte Abschnitte.\n"
         "- Ein Umschalter zeigt den Text auf Englisch oder Deutsch. Er folgt der Sprache der App, solange Sie ihn "
         "nicht ändern.\n"
         "- Schaltflächen starten die Touren eines Abschnitts: die Bereichs-Tour einer Seite oder eine Tour durch "
         "eine Aufgabe.\n"
         "- Tour zu… (auch im Menü Hilfe, Strg+F1) fragt nach einem Thema, etwa Netzwerk, Vorlagen oder Kits, und "
         "listet die Touren und Abschnitte dazu, die besten zuerst: Enter startet die Tour oder öffnet den "
         "Abschnitt.\n\n"
         "Jede Seite, jeder Bereich und jedes Bauteil hat eine kleine (?)-Schaltfläche. Sie öffnet eine kurze "
         "Erklärung, die mit einem Verweis auf die Hilfe endet; er öffnet den passenden Abschnitt hier."),
        see=("app-tours", "app-settings"),
        tours=("help-and-tips", "area:help")),
    # -- features of the app --
    section(
        "app-projects-tabs", "app", ("Projects, tabs and the window", "Projekte, Tabs und das Fenster"),
        ("The current project is shown large at the top, with the state of its `.sandbox-ai.json` (none, "
         "trusted, changed since trusted, not trusted) and how many of its sandboxes run and ask. The switcher "
         "lists the projects sandbox-ai and the app know; Open folder… and New project (wizard)… add one. Start, "
         "Board, Connections, Statistics, Files, Approvals, Sandboxes & sessions, Kits & plugins and Teams follow "
         "the project.\n\n"
         "- Tabs: open projects, sessions and sandboxes are tabs above the pages - shown only on the pages that "
         "follow the project (those above); on the others they are hidden, their place kept. A session tab shows "
         "its state as a dot; its context menu attaches, pauses, resumes or stops. Close with the button, a middle "
         "click or Ctrl+W, move by dragging, cycle with Ctrl+Tab. + opens more.\n"
         "- Window controls: the menus File, View, Go and Help, and buttons in the header bar (minimize, "
         "maximize, full screen with F11, to the tray, quit with Ctrl+Q).\n"
         "- Navigation: Project, Running, Insight, Manage, System. A click on a title folds a section; Only icons "
         "(Ctrl+Shift+B) shrinks it; Ctrl+1 to Ctrl+9 open the first nine pages.\n"
         "- Panels: the Board's parts and inspector and the open questions can float, dock elsewhere or close; "
         "View > Panels brings them back, Reset the panels restores the start.\n\n"
         "Tabs, panels, sizes and the last project come back at the next start. Secrets never go into that "
         "file.",
         "Oben steht groß das aktuelle Projekt mit dem Zustand seiner `.sandbox-ai.json` (keine, vertraut, seit "
         "dem Vertrauen geändert, nicht vertraut) und wie viele seiner Sandboxen laufen und fragen. Die Auswahl "
         "listet die Projekte, die sandbox-ai und die App kennen; Ordner öffnen… und Neues Projekt (Assistent)… "
         "fügen eines hinzu. Start, Tafel, Verbindungen, Statistik, Dateien, Freigaben, Sandboxen & Sitzungen, "
         "Kits & Plugins und Teams folgen dem Projekt.\n\n"
         "- Tabs: Offene Projekte, Sitzungen und Sandboxen sind Tabs über den Seiten - nur auf den Seiten, die dem "
         "Projekt folgen (die oben genannten); auf den anderen sind sie ausgeblendet, ihr Platz bleibt. Ein "
         "Sitzungs-Tab zeigt seinen "
         "Zustand als Punkt; sein Kontextmenü verbindet, pausiert, setzt fort oder stoppt. Schließen mit der "
         "Schaltfläche, Mittelklick oder Strg+W, verschieben per Ziehen, wechseln mit Strg+Tab. + öffnet "
         "weitere.\n"
         "- Fensterknöpfe: die Menüs Datei, Ansicht, Gehe zu und Hilfe sowie Schaltflächen im Kopf (Minimieren, "
         "Maximieren, Vollbild mit F11, in den Infobereich, Beenden mit Strg+Q).\n"
         "- Navigation: Projekt, Laufend, Einblick, Verwalten, System. Ein Klick auf einen Titel klappt einen "
         "Abschnitt ein; Nur Symbole (Strg+Umschalt+B) macht sie schmal; Strg+1 bis Strg+9 öffnen die ersten neun "
         "Seiten.\n"
         "- Bereiche: Teile und Inspektor der Tafel und die offenen Fragen können schweben, anderswo andocken oder "
         "schließen; Ansicht > Bereiche holt sie zurück, Bereiche zurücksetzen stellt den Anfang her.\n\n"
         "Tabs, Bereiche, Größen und das letzte Projekt kommen beim nächsten Start wieder. Geheimnisse gelangen "
         "nie in diese Datei."),
        see=("page-board", "app-settings", "app-tours"),
        tours=("first-start",)),
    section(
        "app-presets-gallery", "app", ("Preset gallery", "Vorlagen-Galerie"),
        ("The gallery offers ready boards to start from. Open it with New from preset… on the Board, Gallery… on "
         "the wizard's Start from page or Gallery… beside Preset on the Start page. Each card shows a picture of "
         "the board, its description, difficulty, security score and how many values are left to fill in. Search "
         "with the box; Enter or a double click creates the board.\n\n"
         "The presets are those of the host the board (or the start) is for, one catalogue per host:\n\n"
         "- Built-in presets of the app: Simple, Development, Research, Local & remote LLM, Privacy & VPN, Teams & "
         "policy.\n"
         "- sandbox-ai's presets: those of the command line on that host (`sandbox-ai presets list --json` there), "
         "built-in, from its extra preset directories and its user's own. When the app and that host's sandbox-ai "
         "have a preset of the same id (tor, local-api, development …), only sandbox-ai's is shown: it is what "
         "`--preset NAME` means there. A host whose sandbox-ai lacks one keeps the app's.\n"
         "- My presets: your own.\n\n"
         "What only you know (a VPN profile, an LLM server, an MCP server, a folder) is a placeholder, asked for "
         "in a short form. The form offers what that host's sandbox-ai knows: its VPN profiles (`vpn list`), LLM "
         "servers (`llm list`) and MCP servers (`mcp list`), never this machine's for another host. Fill in later "
         "keeps it: it stays a problem on the board, and Apply and Start refuse until it is filled in.\n\n"
         "Save the board as a preset… keeps the board shown as your preset (name, description, category; without "
         "the workspaces) in `~/.config/sandbox-ai/app/presets.json`. To change your preset, create a board from "
         "it, change the board, and save it again under the same title: the preset with the same id is replaced. "
         "Export… and Import… pass presets on as JSON files; Delete removes one of yours after asking. A preset "
         "never holds a secret: an imported file is checked and refused when anything looks like a token or "
         "would copy a login in.",
         "Die Galerie bietet fertige Tafeln als Ausgangspunkt. Öffnen Sie sie mit Neu aus Vorlage… auf der Tafel, "
         "Galerie… auf der Seite Ausgangspunkt des Assistenten oder Galerie… neben Vorlage auf der Seite Start. "
         "Jede Karte zeigt ein Bild der Tafel, Beschreibung, Schwierigkeit, Sicherheitswertung und wie viele "
         "Werte noch auszufüllen sind. Suchen Sie im Suchfeld; Enter oder ein Doppelklick legt die Tafel an.\n\n"
         "Die Vorlagen sind die des Hosts, für den die Tafel (oder der Start) ist, ein Katalog je Host:\n\n"
         "- Eingebaute Vorlagen der App: Einfach, Entwicklung, Recherche, Lokale und entfernte LLMs, "
         "Privatsphäre und VPN, Teams und Richtlinien.\n"
         "- Die Vorlagen von sandbox-ai: die der Kommandozeile auf diesem Host (`sandbox-ai presets list --json` "
         "dort), eingebaute, aus seinen zusätzlichen Vorlagen-Ordnern und die eigenen seines Benutzers. Haben die "
         "App und das sandbox-ai dieses Hosts eine Vorlage mit derselben ID (tor, local-api, development …), wird "
         "nur die von sandbox-ai gezeigt: Sie ist, was `--preset NAME` dort bedeutet. Ein Host, dessen sandbox-ai "
         "eine nicht hat, behält die der App.\n"
         "- Meine Vorlagen: Ihre eigenen.\n\n"
         "Was nur Sie wissen (ein VPN-Profil, ein LLM-Server, ein MCP-Server, ein Ordner), ist ein Platzhalter, "
         "nach dem ein kurzes Formular fragt. Das Formular bietet an, was das sandbox-ai dieses Hosts kennt: seine "
         "VPN-Profile (`vpn list`), LLM-Server (`llm list`) und MCP-Server (`mcp list`), für einen anderen Host "
         "nie die dieses Rechners. Später ausfüllen lässt ihn stehen: Er bleibt ein Problem auf der Tafel, und "
         "Übernehmen und Starten verweigern, bis er ausgefüllt ist.\n\n"
         "Tafel als Vorlage speichern… bewahrt die gezeigte Tafel als Ihre Vorlage auf (Name, Beschreibung, "
         "Kategorie; ohne Arbeitsordner) in `~/.config/sandbox-ai/app/presets.json`. Um Ihre Vorlage zu ändern, legen "
         "Sie eine Tafel aus ihr an, ändern die Tafel und speichern sie wieder unter demselben Titel: Die Vorlage "
         "mit derselben ID wird ersetzt. Exportieren… und Importieren… geben Vorlagen als JSON-Dateien weiter; "
         "Löschen entfernt eine Ihrer Vorlagen nach Rückfrage. Eine Vorlage enthält nie ein Geheimnis: Eine "
         "importierte Datei wird geprüft und abgelehnt, wenn etwas wie ein Token aussieht oder sie eine Anmeldung "
         "hineinkopieren würde."),
        cli=("sandbox-ai presets list --json", "sandbox-ai presets show NAME --json", "sandbox-ai vpn list --json",
             "sandbox-ai llm list --json", "sandbox-ai mcp list --json"),
        see=("presets", "page-board", "app-wizard", "page-launch", "hosts"),
        tours=("presets-gallery",)),
    section(
        "app-wizard", "app", ("Wizard", "Assistent"),
        ("The wizard sets up a new or existing project in a few pages and builds the same board as the Board "
         "page. Open it with New project (wizard)… in the project switcher, File > New project… or Wizard… on "
         "the Board.\n\n"
         "The pages: project folder, agent, preset (the gallery) or your own setup, network, connection proxy and "
         "TLS, Git and tokens, MCP servers, devices and limits, and a review with the command line, security "
         "badges, problems, the file and what changes, and the launcher's verdict. Finishing asks first (Cancel "
         "is the default: a trusted file gives every later start of the project its options), then writes and "
         "trusts the file and opens the board. The presets offered are the catalogue of the host the project is "
         "set up for: its sandbox-ai presets, the app's own and yours (see the preset gallery).\n\n"
         "- A token typed here goes to a private file (mode 0600) under `~/.config/sandbox-ai/tokens`; the "
         "project file names only that file.\n"
         "- The project's origin remote is never offered as a default: its `.git/config` is the agent's to "
         "write.\n"
         "- An existing project file is shown, never applied, until it is written and trusted again.\n\n"
         "sandbox-ai has a wizard of its own for the terminal, `sandbox-ai wizard`, with the same rules.",
         "Der Assistent richtet ein neues oder bestehendes Projekt in wenigen Seiten ein und baut dieselbe Tafel "
         "wie die Seite Tafel. Öffnen Sie ihn mit Neues Projekt (Assistent)… in der Projektauswahl, Datei > Neues "
         "Projekt… oder Assistent… auf der Tafel.\n\n"
         "Die Seiten: Projektordner, Agent, Vorlage (die Galerie) oder eigene Einrichtung, Netzwerk, "
         "Verbindungs-Proxy und TLS, Git und Tokens, MCP-Server, Geräte und Limits und eine Prüfung mit "
         "Befehlszeile, Sicherheitsabzeichen, Problemen, der Datei samt Änderungen und dem Urteil des Launchers. "
         "Zum Schluss fragt er zuerst (Abbrechen ist die Vorgabe: Eine vertraute Datei gibt jedem späteren Start "
         "des Projekts ihre Optionen), dann wird die Datei geschrieben und vertraut, und die Tafel öffnet sich. "
         "Die angebotenen Vorlagen sind der Katalog des Hosts, für den das Projekt eingerichtet wird: seine "
         "Vorlagen von sandbox-ai, die eigenen der App und Ihre (siehe die Vorlagen-Galerie).\n\n"
         "- Ein hier getipptes Token kommt in eine private Datei (Modus 0600) unter `~/.config/sandbox-ai/tokens`; "
         "die Projektdatei nennt nur diese Datei.\n"
         "- Das origin-Remote des Projekts wird nie als Vorgabe angeboten: Seine `.git/config` darf der Agent "
         "schreiben.\n"
         "- Eine bestehende Projektdatei wird gezeigt, nie angewendet, bis sie neu geschrieben und vertraut "
         "ist.\n\n"
         "sandbox-ai hat einen eigenen Wizard für das Terminal, `sandbox-ai wizard`, mit denselben Regeln."),
        cli=("sandbox-ai wizard", "sandbox-ai wizard edit", "sandbox-ai wizard trust FILE",
             "sandbox-ai wizard forget FILE"),
        see=("page-board", "app-presets-gallery", "secrets"),
        tours=("own-construct",)),
    section(
        "app-settings", "app", ("Settings, themes and language", "Einstellungen, Farbschemata und Sprache"),
        ("The Settings dialog holds every setting of the app. Open it with All settings… on the System page. A "
         "change is saved at once in `~/.config/sandbox-ai/app/settings.json`.\n\n"
         "- Appearance: theme, language, the help that sets up each new board part, the guided tour at the first "
         "start, the intro (Watch the intro again plays it).\n"
         "- Window: keep answering in the tray, start at login, where commands such as builds and logins run, "
         "desktop notifications and how long they stay, a tab for each start, debug mode.\n"
         "- Polling: how often questions, sandboxes and the connection log are read.\n"
         "- sandbox-ai: the checkout to use (`SANDBOX_AI_HOME` wins when set).\n"
         "- New projects: agent, backend, preset and the board's grid.\n"
         "- Privacy: the agents' telemetry for every start.\n"
         "- Administrator policy: read-only.\n\n"
         "Terminal tabs show a web view (Chromium). Its sandbox needs free user namespaces, and every running "
         "sandbox holds some: before each new tab the app checks in a short-lived process that it can get them, "
         "and when it cannot (`user.max_user_namespaces` reached, or out of memory), the command opens in a "
         "terminal window instead, as Chromium would otherwise end the whole app. End sandboxes you no longer need "
         "or raise the limit (`sysctl user.max_user_namespaces`). \"Use web views without Chromium's sandbox\" "
         "(`QTWEBENGINE_DISABLE_SANDBOX`, off by default, from the next start) lets terminal tabs work anyway, "
         "without the protection Chromium's sandbox gives against a bug in its renderer.\n\n"
         "Ten themes are offered (as the system, light, dark, hacker, cyberpunk, amber, unicorn, Windows 3.11, "
         "Mac, Fallout), also in View > Theme; a theme applies at once. The language (as the system, English, "
         "German) applies at the next start; `SANDBOX_AI_APP_LANGUAGE=en|de` overrides it.",
         "Der Dialog Einstellungen enthält jede Einstellung der App. Öffnen Sie ihn mit Alle Einstellungen… auf "
         "der Seite System. Eine Änderung wird sofort in `~/.config/sandbox-ai/app/settings.json` "
         "gespeichert.\n\n"
         "- Darstellung: Farbschema, Sprache, die Hilfe beim Einrichten neuer Teile der Tafel, die geführte Tour "
         "beim ersten Start, das Intro (Intro noch einmal ansehen spielt es).\n"
         "- Fenster: im Infobereich weiter antworten, Start bei der Anmeldung, wo Befehle wie Builds und "
         "Anmeldungen laufen, Desktop-Benachrichtigungen und wie lange sie bleiben, ein Tab je Start, "
         "Debug-Modus.\n"
         "- Abfragen: wie oft Fragen, Sandboxen und das Verbindungslog gelesen werden.\n"
         "- sandbox-ai: das zu nutzende Checkout (`SANDBOX_AI_HOME` gewinnt, wenn gesetzt).\n"
         "- Neue Projekte: Agent, Backend, Vorlage und das Raster der Tafel.\n"
         "- Datenschutz: die Telemetrie der Agenten für jeden Start.\n"
         "- Richtlinie des Administrators: nur lesbar.\n\n"
         "Terminal-Tabs zeigen eine Web-Ansicht (Chromium). Ihre Sandbox braucht freie User-Namespaces, und jede "
         "laufende Sandbox belegt einige: Vor jedem neuen Tab prüft die App in einem kurzlebigen Prozess, ob sie "
         "welche bekommt; wenn nicht (`user.max_user_namespaces` erreicht oder kein Speicher mehr frei), öffnet "
         "sich der Befehl stattdessen in einem Terminalfenster, weil Chromium sonst die ganze App beenden würde. "
         "Beenden Sie Sandboxen, die Sie nicht mehr brauchen, oder erhöhen Sie die Grenze (`sysctl "
         "user.max_user_namespaces`). „Web-Ansichten ohne Chromiums Sandbox nutzen“ "
         "(`QTWEBENGINE_DISABLE_SANDBOX`, standardmäßig aus, ab dem nächsten Start) lässt Terminal-Tabs trotzdem "
         "arbeiten, ohne den Schutz, den Chromiums Sandbox gegen einen Fehler in seinem Renderer gibt.\n\n"
         "Zehn Farbschemata stehen zur Wahl (wie das System, hell, dunkel, Hacker, Cyberpunk, Bernstein, Einhorn, "
         "Windows 3.11, Mac, Fallout), auch unter Ansicht > Farbschema; ein Farbschema wirkt sofort. Die Sprache "
         "(wie das System, Englisch, Deutsch) gilt ab dem nächsten Start; `SANDBOX_AI_APP_LANGUAGE=en|de` geht "
         "vor."),
        cli=("sandbox-ai defaults show", "--telemetry"),
        see=("page-system", "policy", "logs"),
        tours=("help-and-tips",)),
    section(
        "app-tours", "app", ("Tours and explanations", "Touren und Erklärungen"),
        ("The app explains itself in several ways.\n\n"
         "- Guided tour (Help > Guided tour): one step per page in the order of the navigation, with what the "
         "page is for and what to try there. Back, Next, Close the tour (Esc). It is offered once at the first "
         "start and starts where you left it.\n"
         "- Area tours: a few steps on one page, each outlining the control it is about. Start one with F1 on "
         "the page, Help > Area tours, the navigation's context menu, or Learn this area in the guided tour.\n"
         "- Task tours: a tour through one task across pages, such as a first start, your own board, approvals "
         "and the network, or the preset gallery. Start them from the Help page.\n"
         "- (?) buttons: every page, panel and component has one. It opens a short explanation that ends with "
         "More in Help, which opens the matching section of the Help page.\n"
         "- Explanations on hover: a column title, a label or a setting with a help text shows a sentence about it "
         "when the pointer rests on it, also with the (?) buttons switched off.\n\n"
         "Settings > Appearance turns off the help for new board parts, the tour at the first start and the (?) "
         "buttons.",
         "Die App erklärt sich auf mehreren Wegen.\n\n"
         "- Geführte Tour (Hilfe > Geführte Tour): ein Schritt je Seite in der Reihenfolge der Navigation, mit "
         "dem, wofür die Seite da ist, und was Sie dort probieren. Zurück, Weiter, Tour beenden (Esc). Sie wird "
         "beim ersten Start einmal angeboten und beginnt, wo Sie aufgehört haben.\n"
         "- Bereichs-Touren: wenige Schritte auf einer Seite, jeder umrahmt das Bedienelement, um das es geht. "
         "Starten Sie eine mit F1 auf der Seite, Hilfe > Bereichs-Touren, dem Kontextmenü der Navigation oder "
         "Diesen Bereich kennenlernen in der geführten Tour.\n"
         "- Aufgaben-Touren: eine Tour durch eine Aufgabe über mehrere Seiten, etwa ein erster Start, eine eigene "
         "Tafel, Freigaben und Netzwerk oder die Vorlagen-Galerie. Starten Sie sie auf der Seite Hilfe.\n"
         "- (?)-Schaltflächen: Jede Seite, jeder Bereich und jedes Bauteil hat eine. Sie öffnet eine kurze "
         "Erklärung, die mit einem Verweis auf die Hilfe endet; er öffnet den passenden Abschnitt der Seite "
         "Hilfe.\n"
         "- Erklärungen beim Zeigen: Ein Spaltentitel, eine Beschriftung oder eine Einstellung mit Hilfetext zeigt "
         "einen Satz dazu, wenn der Mauszeiger darauf ruht, auch wenn die (?)-Schaltflächen ausgeschaltet sind.\n\n"
         "Einstellungen > Darstellung schaltet die Hilfe für neue Teile der Tafel, die Tour beim ersten Start und die "
         "(?)-Schaltflächen ab."),
        see=("page-help", "app-settings"),
        tours=("first-start", "help-and-tips")),
    # -- concepts of sandbox-ai --
    section(
        "sandbox", "concepts", ("What a sandbox is", "Was eine Sandbox ist"),
        ("A sandbox runs one agent (Claude Code, Codex or OpenCode) inside two layers: a rootless Podman pod or "
         "Bubblewrap, and inside it Firejail. Before the agent starts, a guard checks that Firejail created its "
         "namespaces, dropped every capability and enabled NoNewPrivs and Seccomp; any failed check aborts the "
         "start.\n\n"
         "- `/work` is the first workspace, the agent's working directory. Further workspaces appear at "
         "`/workspaces/NAME`.\n"
         "- HOME is the sandbox's `/root`: conversations, settings, the copied login. It is kept per start unless "
         "`--no-keep`.\n"
         "- Shared with the sandbox is only what you name: workspaces and mounts, a copy of the agent's login, "
         "your kits (read-only) and the project's memory. Your own `~/.ssh` or `~/.config` stay outside.\n"
         "- The network is default-deny: only the agent's provider is reachable unless you widen it.\n\n"
         "Seeing `root` inside is expected: it maps to your user on the host. A sandbox does not protect against "
         "kernel exploits; use a virtual machine for that. Bubblewrap uses the host's `/usr`, Podman an image per "
         "agent.",
         "Eine Sandbox führt einen Agenten (Claude Code, Codex oder OpenCode) in zwei Schichten aus: einem "
         "rootless Podman-Pod oder Bubblewrap und darin Firejail. Bevor der Agent startet, prüft ein Wächter, dass "
         "Firejail seine Namespaces angelegt, alle Capabilities abgelegt und NoNewPrivs und Seccomp eingeschaltet "
         "hat; jede fehlgeschlagene Prüfung bricht den Start ab.\n\n"
         "- `/work` ist der erste Arbeitsordner, das Arbeitsverzeichnis des Agenten. Weitere Arbeitsordner erscheinen "
         "unter `/workspaces/NAME`.\n"
         "- HOME ist `/root` der Sandbox: Unterhaltungen, Einstellungen, der kopierte Login. Es wird je Start "
         "aufbewahrt, außer mit `--no-keep`.\n"
         "- Geteilt wird nur, was Sie nennen: Arbeitsordner und Mounts, eine Kopie des Logins des Agenten, Ihre Kits "
         "(nur lesbar) und das Memory des Projekts. Ihr eigenes `~/.ssh` oder `~/.config` bleiben draußen.\n"
         "- Das Netzwerk ist standardmäßig zu: Nur der Anbieter des Agenten ist erreichbar, solange Sie nicht "
         "erweitern.\n\n"
         "Dass Sie innen `root` sehen, ist normal: Es entspricht Ihrem Benutzer auf dem Host. Eine Sandbox schützt "
         "nicht vor Kernel-Exploits; nehmen Sie dafür eine virtuelle Maschine. Bubblewrap nutzt `/usr` des Hosts, "
         "Podman ein Image je Agent."),
        cli=("sandbox-ai doctor", "sandbox-ai build AGENT", "--backend", "--probe", "--dry-run"),
        see=("workspaces-mounts", "network", "sessions", "security-modes"),
        tours=("first-start",)),
    section(
        "security-modes", "concepts", ("Security modes", "Sicherheitsmodi"),
        ("A security mode is a preset of options, from `lax` to `paranoid`, chosen with `--mode`. The mode sets "
         "defaults; options may only narrow it. In `high` and `paranoid` an option that would widen the sandbox "
         "is refused, with the laxer mode that would allow it.\n\n"
         "- `lax`: every destination, each connection logged, no questions.\n"
         "- `low`: the development hosts (provider, PyPI, npm, GitHub, the OpenAI and Anthropic APIs).\n"
         "- `medium` (default): the agent's provider only; exactly a start without `--mode`.\n"
         "- `high`: nothing can be opened by options, answers at most for the run, further workspaces read-only, "
         "no rw mounts, devices or GPU, a privacy check of every push, smaller limits.\n"
         "- `paranoid`: as high, every workspace read-only, answers only once, no HOME kept.\n\n"
         "The mode comes from `--mode`, else `\"mode\"` in the project file, else in "
         "`~/.config/sandbox-ai/defaults.json`, else `medium`. The administrator's `min_mode` is a floor. The mode "
         "holds during the run too: `sandbox-ai net allow` opens nothing it refuses. The dry run shows the mode "
         "and where it came from.",
         "Ein Sicherheitsmodus ist eine Voreinstellung von Optionen, von `lax` bis `paranoid`, gewählt mit "
         "`--mode`. Der Modus setzt Vorgaben; Optionen dürfen sie nur enger machen. In `high` und `paranoid` wird "
         "eine Option, die die Sandbox erweitern würde, abgelehnt, mit dem laxeren Modus, der sie erlauben "
         "würde.\n\n"
         "- `lax`: jedes Ziel, jede Verbindung protokolliert, keine Fragen.\n"
         "- `low`: die Entwicklungs-Hosts (Anbieter, PyPI, npm, GitHub, die OpenAI- und Anthropic-APIs).\n"
         "- `medium` (Standard): nur der Anbieter des Agenten; genau ein Start ohne `--mode`.\n"
         "- `high`: Optionen öffnen nichts, Antworten höchstens für den Lauf, weitere Arbeitsordner nur lesbar, keine "
         "rw-Mounts, Geräte oder GPU, Datenschutzprüfung jedes Pushs, kleinere Limits.\n"
         "- `paranoid`: wie high, jeder Arbeitsordner nur lesbar, Antworten nur einmal, kein HOME bleibt.\n\n"
         "Der Modus kommt aus `--mode`, sonst aus `\"mode\"` der Projektdatei, sonst aus "
         "`~/.config/sandbox-ai/defaults.json`, sonst `medium`. `min_mode` des Administrators ist eine Untergrenze. "
         "Der Modus gilt auch während des Laufs: `sandbox-ai net allow` öffnet nichts, was er ablehnt. Der "
         "Probelauf zeigt den Modus und woher er kommt."),
        cli=("--mode lax|low|medium|high|paranoid", "--mode-at-least", "sandbox-ai doctor"),
        see=("network", "approvals", "taint", "canaries", "policy"),
        tours=("first-start",)),
    section(
        "workspaces-mounts", "concepts", ("Workspaces and mounts", "Arbeitsordner und Mounts"),
        ("A workspace is a host folder the agent works in. `--workspace PATH` appears at `/work` and is writable "
         "unless `--workspace-ro`. Repeat the option for more: the first is always `/work`, every further one "
         "appears at `/workspaces/NAME`.\n\n"
         "- `--mount SOURCE:DESTINATION:ro|rw` adds a folder elsewhere; destinations lie under `/work`, `/mnt` or "
         "`/opt`. Sockets and device files are refused.\n"
         "- A workspace may not be or contain your HOME, `/`, `~/.config`, `~/.ssh`, `~/.local` and similar "
         "places, or sandbox-ai itself.\n"
         "- sandbox-ai records every tree a sandbox could write; a path through a symlink inside such a tree is "
         "refused.\n\n"
         "The project's `.sandbox-ai.json` lives in the first workspace and takes effect only once you trust it. "
         "After a run, review a workspace's `.git/hooks` and `.git/config` before you use git there: they run "
         "code on the host. In the app the first workspace is the current project's folder; the Board shows "
         "workspaces and mounts as parts.",
         "Ein Arbeitsordner ist ein Ordner des Hosts, in dem der Agent arbeitet. `--workspace PFAD` erscheint unter "
         "`/work` und ist beschreibbar, außer mit `--workspace-ro`. Wiederholen Sie die Option für mehr: Der erste "
         "ist immer `/work`, jeder weitere erscheint unter `/workspaces/NAME`.\n\n"
         "- `--mount QUELLE:ZIEL:ro|rw` fügt einen Ordner anderswo hinzu; Ziele liegen unter `/work`, `/mnt` oder "
         "`/opt`. Sockets und Gerätedateien werden abgelehnt.\n"
         "- Ein Arbeitsordner darf nicht Ihr HOME, `/`, `~/.config`, `~/.ssh`, `~/.local` und ähnliche Orte oder "
         "sandbox-ai selbst sein oder enthalten.\n"
         "- sandbox-ai merkt sich jeden Baum, den eine Sandbox schreiben konnte; ein Pfad über einen Symlink in "
         "einem solchen Baum wird abgelehnt.\n\n"
         "Die `.sandbox-ai.json` des Projekts liegt im ersten Arbeitsordner und wirkt erst, wenn Sie ihr vertrauen. "
         "Prüfen Sie nach einem Lauf `.git/hooks` und `.git/config` eines Arbeitsordners, bevor Sie dort git benutzen: "
         "Sie führen Code auf dem Host aus. In der App ist der erste Arbeitsordner der Ordner des "
         "aktuellen Projekts; die Tafel zeigt Arbeitsordner und Mounts als Teile."),
        cli=("--workspace", "--workspace-ro", "--mount", "--state-dir", "sandbox-ai wizard trust FILE"),
        see=("sandbox", "page-files", "page-board"),
        tours=()),
    section(
        "sessions", "concepts", ("Starts and sessions", "Starts und Sitzungen"),
        ("Every start of an agent has a name: what `--name` gives, else a codename of three words such as "
         "`brisk-otter-lantern` (`--name-style project` or the default `name_style` `project` names it "
         "`AGENT-PROJECT-xxxx` instead). `sandbox-ai list` lists it, with its workspace. An interactive start "
         "runs as a session: the agent runs in tmux inside the sandbox "
         "(windows `agent` and `shell`) and your terminal attaches.\n\n"
         "- Ctrl-b d or closing the terminal detaches; the agent works on. `sandbox-ai attach NAME` brings it "
         "back.\n"
         "- `pause` freezes every process, `resume` thaws it.\n"
         "- `stop` ends it; HOME, options and logs are kept. `resume` on a stopped start creates a new sandbox "
         "with the same HOME, and the agent continues its last conversation.\n"
         "- `rename` changes the name, the run id stays; without a new name it gives a fresh codename. `delete` "
         "removes a stopped start with its HOME.\n\n"
         "Tasks (`-p`, `codex exec`, `opencode run`, a start without a terminal) run in the foreground and cannot "
         "be attached. Running processes are not saved: only HOME and the workspaces survive a stop. Before a "
         "kept HOME is reborn, the launcher names every change in its start files; look before you go on. "
         "`--no-keep` discards the HOME at the end.",
         "Jeder Start eines Agenten hat einen Namen: was `--name` angibt, sonst einen Codenamen aus drei Wörtern "
         "wie `brisk-otter-lantern` (`--name-style project` oder die Vorgabe `name_style` `project` nennt ihn "
         "stattdessen `AGENT-PROJEKT-xxxx`). `sandbox-ai list` listet ihn mit seinem Arbeitsordner. Ein "
         "interaktiver Start läuft als Sitzung: Der Agent läuft in tmux in der "
         "Sandbox (Fenster `agent` und `shell`), und Ihr Terminal verbindet sich.\n\n"
         "- Strg-b d oder das Schließen des Terminals löst sich; der Agent arbeitet weiter. `sandbox-ai attach "
         "NAME` holt ihn zurück.\n"
         "- `pause` friert jeden Prozess ein, `resume` taut ihn auf.\n"
         "- `stop` beendet ihn; HOME, Optionen und Protokolle bleiben. `resume` auf einem gestoppten Start legt eine "
         "neue Sandbox mit demselben HOME an, und der Agent setzt seine letzte Unterhaltung fort.\n"
         "- `rename` ändert den Namen, die Run-ID bleibt; ohne neuen Namen gibt es einen frischen Codenamen. "
         "`delete` entfernt einen gestoppten Start samt HOME.\n\n"
         "Aufgaben (`-p`, `codex exec`, `opencode run`, ein Start ohne Terminal) laufen im Vordergrund und lassen "
         "sich nicht verbinden. Laufende Prozesse werden nicht gesichert: Nur HOME und die Arbeitsordner überstehen "
         "einen Stopp. Bevor ein aufbewahrtes HOME wiedergeboren wird, nennt der Launcher jede Änderung seiner "
         "Startdateien; sehen Sie hin, bevor Sie weitermachen. `--no-keep` verwirft das HOME am Ende."),
        cli=("sandbox-ai list", "sandbox-ai attach NAME", "sandbox-ai pause NAME", "sandbox-ai resume NAME",
             "sandbox-ai stop NAME", "sandbox-ai rename OLD NEW", "sandbox-ai rename OLD --codename",
             "sandbox-ai delete NAME", "sandbox-ai resume NAME --review-home", "sandbox-ai gc", "--name",
             "--name-style", "--no-keep"),
        see=("page-sandboxes", "page-terminals", "sandbox"),
        tours=()),
    section(
        "network", "concepts", ("Network and allow-list", "Netzwerk und Freigabeliste"),
        ("Each sandbox has its own network with a default-deny firewall. Only the agent's provider hosts are "
         "resolved at the start, pinned and allowed on HTTPS; there is no DNS inside by default.\n\n"
         "- `--profile NAME` opens a bundle: `development` (provider, PyPI, GitHub, the OpenAI and Anthropic "
         "APIs), `frontier`, `dns`, `egress-all`, `offline`.\n"
         "- Presets for known hosts: `--allow-pypi`, `--allow-github`, `--allow-gitlab`, `--allow-npm` and "
         "more.\n"
         "- `--allow-host NAME` pins a name on HTTPS, `--allow-tcp IP:PORT` one port, `--allow IP_OR_CIDR` "
         "everything to a destination.\n"
         "- `--allow-host-port PORT` (Podman) reaches a service on the host's own loopback, such as a local Git "
         "server.\n"
         "- `--dns` gives real DNS, `--offline` no network, `--egress-all-allow` every destination.\n\n"
         "A running sandbox can get more or less without a restart: `sandbox-ai net allow` and `net revoke`, "
         "with `--keep` also for the next starts. Every widening is a decision: `--dns` can carry data out in "
         "names, and IP filtering cannot tell apart domains that share CDN addresses. A destination not allowed "
         "becomes a question on the Approvals page.",
         "Jede Sandbox hat ihr eigenes Netzwerk mit einer standardmäßig sperrenden Firewall. Nur die Hosts des "
         "Anbieters werden beim Start aufgelöst, gepinnt und für HTTPS erlaubt; innen gibt es standardmäßig kein "
         "DNS.\n\n"
         "- `--profile NAME` öffnet ein Bündel: `development` (Anbieter, PyPI, GitHub, die OpenAI- und "
         "Anthropic-APIs), `frontier`, `dns`, `egress-all`, `offline`.\n"
         "- Voreinstellungen für bekannte Hosts: `--allow-pypi`, `--allow-github`, `--allow-gitlab`, `--allow-npm` "
         "und weitere.\n"
         "- `--allow-host NAME` pinnt einen Namen für HTTPS, `--allow-tcp IP:PORT` einen Port, `--allow "
         "IP_ODER_CIDR` alles zu einem Ziel.\n"
         "- `--allow-host-port PORT` (Podman) erreicht einen Dienst auf dem Loopback des Hosts, etwa einen lokalen "
         "Git-Server.\n"
         "- `--dns` gibt echtes DNS, `--offline` kein Netzwerk, `--egress-all-allow` jedes Ziel.\n\n"
         "Eine laufende Sandbox bekommt mehr oder weniger ohne Neustart: `sandbox-ai net allow` und `net revoke`, "
         "mit `--keep` auch für die nächsten Starts. Jede Erweiterung ist eine Entscheidung: `--dns` kann Daten in "
         "Namen hinaustragen, und IP-Filter unterscheiden keine Domains, die sich CDN-Adressen teilen. Ein nicht "
         "erlaubtes Ziel wird zu einer Frage auf der Seite Freigaben."),
        cli=("--profile", "--allow-host", "--allow-tcp", "--allow", "--allow-host-port", "--dns", "--offline",
             "--egress-all-allow", "--no-provider-defaults", "--subnet", "sandbox-ai net allow",
             "sandbox-ai net revoke", "sandbox-ai net show", "sandbox-ai logs"),
        see=("proxy", "approvals", "vpn-tor", "page-connections", "page-sandboxes"),
        tours=("approvals-network",)),
    section(
        "proxy", "concepts", ("Connection proxy", "Verbindungs-Proxy"),
        ("Every TCP connection of the agent passes a proxy that starts with the sandbox. It is on by default; "
         "`--no-proxy` switches it off.\n\n"
         "- Transparent: programs need no proxy settings, and what the network options do not allow is refused "
         "at once.\n"
         "- TLS inspection: TLS is opened with a CA made for this run alone, which the agent trusts; the proxy "
         "checks the real server against the system's CAs. `--no-inspect HOST` passes a host through unopened.\n"
         "- Logged: every request with method, URL, status, headers and bodies, credentials redacted.\n"
         "- Push check: a Git push over HTTP(S) waits until the privacy check has seen it.\n"
         "- Rules per host: `--allow-http 'HOST METHODS PATH'` lets only matching requests through; the rest "
         "gets a 403.\n"
         "- Tokens kept out: with `--token-proxy`, Git tokens go only to the proxy, which adds them to Git's own "
         "requests. `--api-key`, `--llm` and publish logins work the same way.\n\n"
         "Programs that pin certificates reject the session CA; the log shows `tls-refused` with a hint. A "
         "determined agent can encode data past any pattern check, but the log records what was sent.",
         "Jede TCP-Verbindung des Agenten läuft durch einen Proxy, der mit der Sandbox startet. Er ist "
         "standardmäßig an; `--no-proxy` schaltet ihn ab.\n\n"
         "- Transparent: Programme brauchen keine Proxy-Einstellungen, und was die Netzwerkoptionen nicht "
         "erlauben, wird sofort abgewiesen.\n"
         "- TLS-Inspektion: TLS wird mit einer nur für diesen Lauf erzeugten CA geöffnet, der der Agent vertraut; "
         "der Proxy prüft den echten Server gegen die CAs des Systems. `--no-inspect HOST` lässt einen Host "
         "ungeöffnet durch.\n"
         "- Protokolliert: jede Anfrage mit Methode, URL, Status, Headern und Bodies, Anmeldedaten geschwärzt.\n"
         "- Push-Prüfung: Ein Git-Push über HTTP(S) wartet, bis die Datenschutzprüfung ihn gesehen hat.\n"
         "- Regeln je Host: `--allow-http 'HOST METHODEN PFAD'` lässt nur passende Anfragen durch; der Rest "
         "bekommt ein 403.\n"
         "- Tokens bleiben draußen: Mit `--token-proxy` gehen Git-Tokens nur an den Proxy, der sie in Gits eigene "
         "Anfragen einsetzt. `--api-key`, `--llm` und Publish-Logins funktionieren genauso.\n\n"
         "Programme, die Zertifikate pinnen, lehnen die CA der Sitzung ab; das Protokoll zeigt `tls-refused` mit einem "
         "Hinweis. Ein entschlossener Agent kann Daten an jeder Musterprüfung vorbei kodieren, aber das Protokoll hält "
         "fest, was gesendet wurde."),
        cli=("--no-proxy", "--no-inspect", "--allow-http", "--token-proxy", "--api-key", "--log-dir"),
        see=("network", "privacy-check", "rulesets", "secrets", "logs", "page-connections"),
        tours=("approvals-network",)),
    section(
        "approvals", "concepts", ("Approvals", "Freigaben"),
        ("With the proxy on, a sandbox asks you instead of failing silently (`--ask on`, the default). It asks "
         "about a connection or name lookup the options do not allow, a push the privacy check has warnings "
         "about, a request or MCP tool call an `ask` rule holds, a device a device rule asks about, and a URL to "
         "open in your browser.\n\n"
         "- once allows this one connection, run until the sandbox ends, always also every later start of the "
         "same project.\n"
         "- deny refuses it for this run; always blocked refuses it at every later start.\n"
         "- A question nobody answers is refused after 60 seconds.\n\n"
         "Questions appear as desktop notifications, in the app and in `sandbox-ai approvals`. The name in a "
         "connection question is the agent's claim: what you allow is the address, unless sandbox-ai can prove "
         "the name. `--ask off` refuses everything the options do not allow. The security mode `high` caps "
         "answers at run, `paranoid` at once, and the administrator's `max_approval_scope` can cap them further.\n\n"
         "`--ask allow` (Always allow everything) asks nothing: every question is allowed at once, for the run, and "
         "recorded as \"allowed automatically\" in the approvals history and the connection log. What rules deny "
         "and the privacy check's blocking findings stay refused, and a canary alarm still waits for you. With a "
         "strict mode you chose, its questions are answered too; where a preset or the administrator set the mode, "
         "it is refused.",
         "Mit Proxy fragt eine Sandbox nach, statt still zu scheitern (`--ask on`, der Standard). Sie fragt nach "
         "einer Verbindung oder Namensauflösung, die die Optionen nicht erlauben, einem Push, zu dem die "
         "Datenschutzprüfung Warnungen hat, einer Anfrage oder einem MCP-Aufruf, den eine `ask`-Regel anhält, "
         "einem Gerät, nach dem eine Geräteregel fragt, und einer URL, die in Ihrem Browser öffnen soll.\n\n"
         "- einmal erlaubt diese eine Verbindung, für diesen Lauf bis zum Ende der Sandbox, immer auch jeden "
         "späteren Start desselben Projekts.\n"
         "- Ablehnen weist sie für diesen Lauf ab; Immer blockieren weist sie bei jedem späteren Start ab.\n"
         "- Eine Frage ohne Antwort wird nach 60 Sekunden abgelehnt.\n\n"
         "Fragen erscheinen als Desktop-Benachrichtigung, in der App und in `sandbox-ai approvals`. Der Name in "
         "einer Verbindungsfrage ist eine Angabe des Agenten: Erlaubt wird die Adresse, außer sandbox-ai kann den "
         "Namen belegen. `--ask off` weist alles ab, was die Optionen nicht erlauben. Der Sicherheitsmodus `high` "
         "begrenzt Antworten auf den Lauf, `paranoid` auf einmal, und `max_approval_scope` des Administrators kann "
         "sie weiter begrenzen.\n\n"
         "`--ask allow` (Immer alles erlauben) fragt nichts: Jede Frage wird sofort erlaubt, für den Lauf, und als "
         "\"allowed automatically\" im Verlauf der Freigaben und im Verbindungsprotokoll verzeichnet. Was Regeln "
         "ablehnen und Funde der Datenschutzprüfung, die einen Push stoppen, bleiben abgewiesen, und ein Canary-Alarm "
         "wartet weiter auf Sie. Mit einem strengen Modus, den Sie gewählt haben, werden auch dessen Fragen "
         "beantwortet; hat ein Preset oder der Administrator den Modus gesetzt, wird es abgelehnt."),
        cli=("--ask on|off|allow", "sandbox-ai approvals list", "sandbox-ai approvals allow ID --scope once|run|always",
             "sandbox-ai approvals remembered", "sandbox-ai approvals forget PROJECT ENTRY"),
        see=("page-approvals", "network", "rulesets", "security-modes"),
        tours=("approvals-network",)),
    section(
        "taint", "concepts", ("The run's taint", "Der Taint des Laufs"),
        ("Once untrusted content has entered the agent's context, any later action may follow instructions "
         "hidden in it. sandbox-ai cannot see how data flows inside the model, so it marks the whole run instead: "
         "the run is tainted.\n\n"
         "- A ruleset names sources of untrusted content under `\"untrusted\"`: responses of hosts, results of "
         "MCP tools, the agent's own tools. A rule with a `when` condition on `tainted` holds only after such "
         "content arrived.\n"
         "- In `high`, content from any host but the provider taints the run, and every MCP tool call then asks. "
         "In `paranoid`, MCP results and the agent's WebSearch and WebFetch taint it too, and its shell, file "
         "edits and fetches ask as well.\n"
         "- The agent's own provider is never a source.\n\n"
         "A question held by the taint says why and since when. The connection log, Activity and `sandbox-ai net "
         "show` show the taint. Only you take it back, with `sandbox-ai net untaint`; a new run starts untainted. "
         "The taint is coarse by design: every later write asks, however unrelated.",
         "Sobald nicht vertrauenswürdiger Inhalt in den Kontext des Agenten gelangt ist, kann jede spätere Aktion "
         "darin versteckten Anweisungen folgen. sandbox-ai sieht nicht, wie Daten im Modell fließen, und markiert "
         "deshalb den ganzen Lauf: Der Lauf bekommt einen Taint.\n\n"
         "- Ein Regelwerk nennt Quellen nicht vertrauenswürdigen Inhalts unter `\"untrusted\"`: Antworten von "
         "Hosts, Ergebnisse von MCP-Werkzeugen, die eigenen Werkzeuge des Agenten. Regeln mit der Bedingung "
         "`\"when\"` auf `tainted` gelten erst, wenn solcher Inhalt angekommen ist.\n"
         "- In `high` gibt Inhalt jedes Hosts außer dem Anbieter dem Lauf einen Taint, danach fragt jeder "
         "MCP-Aufruf. In `paranoid` tun das auch MCP-Ergebnisse und WebSearch und WebFetch des Agenten, und "
         "danach fragen auch seine Shell, Dateiänderungen und Abrufe.\n"
         "- Der eigene Anbieter des Agenten ist nie eine Quelle.\n\n"
         "Eine Frage, die der Taint anhält, sagt warum und seit wann. Verbindungslog, Aktivität und `sandbox-ai "
         "net show` zeigen den Taint. Nur Sie nehmen ihn zurück, mit `sandbox-ai net untaint`; ein neuer Lauf "
         "beginnt ohne Taint. Der Taint ist absichtlich grob: Jedes spätere Schreiben fragt, wie unverwandt es "
         "auch ist."),
        cli=("sandbox-ai net untaint RUN", "sandbox-ai net show", "--rules"),
        see=("rulesets", "security-modes", "approvals", "page-activity"),
        tours=()),
    section(
        "canaries", "concepts", ("Canary files", "Köderdateien (Canaries)"),
        ("A canary is a decoy: a file that looks like a credential, planted in the sandbox for one run. Nothing "
         "legitimate needs it, so whoever opens it, and above all whoever sends its contents somewhere, is worth "
         "knowing about: a prompt injection, a malicious install script, a tool that uploads your HOME.\n\n"
         "- `credentials` (on by default): files such as `~/.aws/credentials`, SSH keys, `~/.netrc`, "
         "`~/.git-credentials` in the sandbox HOME.\n"
         "- `home` (on by default): `~/Documents/passwords.txt`, `~/.kube/config`.\n"
         "- `workspace` (off): a `.env` in each workspace, since it changes your project folder.\n\n"
         "Each run gets its own random tokens in the shape of real ones, which no service accepts. A read is "
         "reported (in `high` asked about, with the sandbox paused meanwhile); a token leaving through the proxy "
         "is refused and stops the run in the default mode. `sandbox-ai canaries resume RUN` continues a sandbox "
         "a canary paused. A token cut into pieces, encrypted or sent past the proxy is not found.",
         "Ein Köder ist eine Attrappe: eine Datei, die wie Anmeldedaten aussieht und für einen Lauf in die Sandbox "
         "gelegt wird. Nichts Legitimes braucht sie; wer sie öffnet und vor allem wer ihren Inhalt irgendwohin "
         "schickt, verdient Aufmerksamkeit: eine Prompt-Injection, ein bösartiges Installationsskript, ein "
         "Werkzeug, das Ihr HOME hochlädt.\n\n"
         "- `credentials` (standardmäßig an): Dateien wie `~/.aws/credentials`, SSH-Schlüssel, `~/.netrc`, "
         "`~/.git-credentials` im HOME der Sandbox.\n"
         "- `home` (standardmäßig an): `~/Documents/passwords.txt`, `~/.kube/config`.\n"
         "- `workspace` (aus): eine `.env` in jedem Arbeitsordner, da sie Ihren Projektordner ändert.\n\n"
         "Jeder Lauf bekommt eigene zufällige Tokens in der Form echter, die kein Dienst annimmt. Ein Lesen wird "
         "gemeldet (in `high` erfragt, die Sandbox solange pausiert); ein Token, das durch den Proxy hinaus will, "
         "wird im Standardmodus abgewiesen und stoppt den Lauf. `sandbox-ai canaries resume RUN` setzt eine "
         "Sandbox fort, die ein Köder pausiert hat. Ein zerteiltes, verschlüsseltes oder am Proxy vorbei "
         "gesendetes Token wird nicht gefunden."),
        cli=("--canaries credentials,home,workspace", "--no-canaries", "sandbox-ai canaries list",
             "sandbox-ai canaries resume RUN", "sandbox-ai canaries log [RUN] [--json]"),
        see=("security-modes", "rulesets", "policy", "proxy", "page-activity"),
        tours=("area:activity",)),
    section(
        "presets", "concepts", ("Presets of sandbox-ai", "Vorlagen von sandbox-ai"),
        ("A preset of sandbox-ai is a ready setup to start an agent with: launcher options, MCP servers, an "
         "optional ruleset and a first task, with placeholders for what only you know (a folder, a port, a "
         "device, an LLM server).\n\n"
         "- `sandbox-ai presets list` shows the built-in presets and your own, `presets show NAME` what one sets "
         "and asks for, `presets check NAME` what is missing before it can start.\n"
         "- Start with `--preset NAME --preset-value KEY=VALUE`. The command line wins over the preset; the "
         "project file, your defaults, the security mode and the policy apply as always. A preset's `--mode` is a "
         "floor.\n"
         "- Your own presets are JSON files in `~/.config/sandbox-ai/presets/`, outside every workspace. One with "
         "the id of a built-in preset replaces it.\n\n"
         "The app has presets of its own: ready boards and your saved boards in "
         "`~/.config/sandbox-ai/app/presets.json`. The gallery shows both kinds: sandbox-ai's presets in their own "
         "category, the app's in the others and under My presets. Neither kind ever holds a secret, and neither "
         "may copy a login in.",
         "Eine Vorlage von sandbox-ai ist eine fertige Einrichtung, um einen Agenten zu starten: Optionen des "
         "Launchers, MCP-Server, ein optionales Regelwerk und eine erste Aufgabe, mit Platzhaltern für das, was "
         "nur Sie wissen (ein Ordner, ein Port, ein Gerät, ein LLM-Server).\n\n"
         "- `sandbox-ai presets list` zeigt die eingebauten und Ihre eigenen Vorlagen, `presets show NAME`, was "
         "eines setzt und erfragt, `presets check NAME`, was fehlt, bevor es starten kann.\n"
         "- Starten Sie mit `--preset NAME --preset-value SCHLÜSSEL=WERT`. Die Kommandozeile gewinnt über die "
         "Vorlage; Projektdatei, Ihre Vorgaben, Sicherheitsmodus und Richtlinie gelten wie immer. Das `--mode` "
         "einer Vorlage ist eine Untergrenze.\n"
         "- Eigene Vorlagen sind JSON-Dateien in `~/.config/sandbox-ai/presets/`, außerhalb jedes Arbeitsordners. Eines "
         "mit der ID eines eingebauten ersetzt dieses.\n\n"
         "Die App hat eigene Vorlagen: fertige Tafeln und Ihre gespeicherten Tafeln in "
         "`~/.config/sandbox-ai/app/presets.json`. Die Galerie zeigt beide Arten: die Vorlagen von sandbox-ai in "
         "einer eigenen Kategorie, die Vorlagen der App in den anderen und unter Meine Vorlagen. Keine der beiden "
         "enthält je ein Geheimnis, und keine darf eine Anmeldung hineinkopieren."),
        cli=("sandbox-ai presets list", "sandbox-ai presets show NAME", "sandbox-ai presets check NAME",
             "--preset", "--preset-value"),
        see=("app-presets-gallery", "security-modes", "mcp", "rulesets"),
        tours=("presets-gallery",)),
    section(
        "kits", "concepts", ("Kits, memory, plugins and hooks", "Kits, Memory, Plugins und Hooks"),
        ("Kits bring the agent's skills, agents, commands, prompts and a memory template into a sandbox, without "
         "exposing your own `~/.claude`, `~/.codex` or `~/.config/opencode`.\n\n"
         "- The default kit is part of every sandbox of an agent; `--kit NAME` adds named kits, "
         "`--no-default-kit` leaves the default out. sandbox-ai's built-in skill `sandbox-ai` explains the "
         "sandbox to the agent.\n"
         "- Entries are copied into a read-only snapshot: the agent can use, not change them. The same name in "
         "two kits stops the start.\n"
         "- Project memory: the agent's memory file is kept per project and taken back after the run; "
         "`--no-memory` starts without it.\n"
         "- Claude plugins are declared in a kit and installed into a plugin store with `sandbox-ai plugins "
         "claude`; a sandbox enables exactly the plugins its kits list.\n"
         "- The Kits page is a marketplace browser: every marketplace on that host (sandbox-ai's own, the kits', the "
         "plugin store's, Claude's and Codex's) with its plugins and their state; add a plugin to a kit or the "
         "default kit, enable, disable or remove it there; add, remove or import a marketplace. The agents' own "
         "configuration is only read.\n"
         "- Disabled: a kit can keep a plugin declared and installed but off; the last kit of a start that names a "
         "plugin decides, so a kit given with --kit can turn off one the default kit enables.\n"
         "- A plugin list comes from a copy fetched before; Fetch the plugin lists runs `sandbox-ai plugins claude "
         "--marketplaces-only` (network only to the marketplaces' hosts, installs nothing).\n"
         "- OpenCode has no marketplaces (npm plugins); Codex plugins don't go into kits yet; a Codex marketplace in "
         "Claude's format can be imported for Claude; a marketplace in a local folder cannot go into a kit.\n"
         "- Hooks in a kit run only after `sandbox-ai kits trust-hooks KIT` and only while nothing in them "
         "changes.\n\n"
         "A plugin or hook runs code in every sandbox that enables it; install and approve only what you trust.",
         "Kits bringen Skills, Agenten, Befehle, Prompts und eine Memory-Vorlage des Agenten in eine Sandbox, "
         "ohne Ihr eigenes `~/.claude`, `~/.codex` oder `~/.config/opencode` freizugeben.\n\n"
         "- Das Standard-Kit gehört zu jeder Sandbox eines Agenten; `--kit NAME` fügt benannte Kits hinzu, "
         "`--no-default-kit` lässt das Standard-Kit weg. Der eingebaute Skill `sandbox-ai` erklärt dem Agenten die "
         "Sandbox.\n"
         "- Einträge werden in einen schreibgeschützten Schnappschuss kopiert: Der Agent kann sie nutzen, nicht "
         "ändern. Derselbe Name in zwei Kits stoppt den Start.\n"
         "- Projekt-Memory: Die Memory-Datei des Agenten wird je Projekt aufbewahrt und nach dem Lauf "
         "zurückgenommen; `--no-memory` startet ohne.\n"
         "- Claude-Plugins werden in einem Kit erklärt und mit `sandbox-ai plugins claude` in einen Plugin-Speicher "
         "installiert; eine Sandbox aktiviert genau die Plugins, die ihre Kits nennen.\n"
         "- Die Seite Kits ist ein Marktplatz-Browser: jeder Marktplatz auf diesem Host (der eigene von sandbox-ai, "
         "die der Kits, der des Plugin-Speichers, die von Claude und Codex) mit seinen Plugins und deren Zustand; "
         "ein Plugin zu einem Kit oder dem Standard-Kit hinzufügen, dort einschalten, ausschalten oder entfernen; "
         "einen Marktplatz hinzufügen, entfernen oder importieren. Die eigene Konfiguration der Agenten wird nur "
         "gelesen.\n"
         "- Ausgeschaltet: Ein Kit kann ein Plugin angegeben und installiert, aber aus halten; das letzte Kit eines "
         "Starts, das ein Plugin nennt, entscheidet, so kann ein mit --kit gegebenes Kit eines ausschalten, das das "
         "Standard-Kit einschaltet.\n"
         "- Eine Plugin-Liste stammt aus einer früher geholten Kopie; Plugin-Listen holen führt `sandbox-ai plugins "
         "claude --marketplaces-only` aus (Netzwerk nur zu den Hosts der Marktplätze, installiert nichts).\n"
         "- OpenCode hat keine Marktplätze (npm-Plugins); Plugins von Codex kommen noch nicht in Kits; ein "
         "Marktplatz von Codex im Format von Claude lässt sich für Claude importieren; ein Marktplatz in einem "
         "lokalen Ordner kann nicht in ein Kit.\n"
         "- Hooks in einem Kit laufen erst nach `sandbox-ai kits trust-hooks KIT` und nur, solange sich nichts an "
         "ihnen ändert.\n\n"
         "Ein Plugin oder Hook führt Code in jeder Sandbox aus, die es aktiviert; installieren und geben Sie nur "
         "frei, was Sie für vertrauenswürdig halten."),
        cli=("--kit", "--kit-dir", "--no-default-kit", "--no-memory", "--no-plugins", "sandbox-ai kits",
             "sandbox-ai kits trust-hooks KIT", "sandbox-ai plugins claude", "sandbox-ai plugins marketplaces",
             "sandbox-ai plugins claude --marketplaces-only", "sandbox-ai kits enable-plugin",
             "sandbox-ai kits disable-plugin", "sandbox-ai kits import-marketplace"),
        see=("page-kits", "rulesets"),
        tours=()),
    section(
        "rulesets", "concepts", ("Rulesets", "Regelwerke"),
        ("The network options decide which hosts the agent reaches. Rulesets decide what it may do there: they "
         "allow, ask or deny each HTTP request (by host, method, path, query, headers and body), each MCP tool "
         "call (by tool and arguments), each device, and the agent's own tools.\n\n"
         "Rulesets come from five places, each a JSON file of the same format:\n\n"
         "- the administrator's policy, key `rules`;\n"
         "- `~/.config/sandbox-ai/rules.json`, at every start;\n"
         "- `--rules FILE`, for one start (outside every workspace);\n"
         "- `--allow-http 'HOST METHODS PATH'`;\n"
         "- the trusted project file, key `rules`.\n\n"
         "Each ruleset decides on its own and the strictest decision wins: deny over ask over allow. No rule opens "
         "the firewall. Rules are enforced by the connection proxy and the relay in front of each MCP server, "
         "which the agent cannot reach; rules for the agent's own tools go through its hooks, a guard rail, not a "
         "boundary. `--dry-run` with `--rules` lists the rulesets and stops on errors. On the Board, rulesets are "
         "a part of their own.\n\n"
         "A short example, `~/.config/sandbox-ai/github.json`: `--allow-github` opens every request to "
         "api.github.com; this keeps gists and issue writes out and asks before a release:\n\n"
         "```json\n"
         "{\"version\": 1, \"http\": [\n"
         "  {\"id\": \"no-gists\", \"host\": \"api.github.com\", \"path\": \"/gists/**\", \"decision\": \"deny\"},\n"
         "  {\"id\": \"no-issue-writes\", \"host\": \"api.github.com\", \"methods\": [\"POST\", \"PATCH\", \"PUT\", "
         "\"DELETE\"], \"path\": \"/repos/*/*/issues/**\", \"decision\": \"deny\"},\n"
         "  {\"id\": \"releases\", \"host\": \"api.github.com\", \"methods\": \"POST\", \"path\": "
         "\"/repos/*/*/releases\", \"decision\": \"ask\"}]}\n"
         "```\n\n"
         "In the app, the ruleset files field (the Approvals and rules part on the Board, Ruleset file on the Start "
         "page) has New from example…, which writes this starter (and an MCP example) to a file you choose outside "
         "every workspace, and Check, which asks the launcher whether it reads the files.",
         "Die Netzwerkoptionen entscheiden, welche Hosts der Agent erreicht. Regelwerke entscheiden, was er dort "
         "darf: Sie erlauben, erfragen oder verbieten jede HTTP-Anfrage (nach Host, Methode, Pfad, Query, Headern "
         "und Body), jeden MCP-Aufruf (nach Werkzeug und Argumenten), jedes Gerät und die eigenen Werkzeuge des "
         "Agenten.\n\n"
         "Regelwerke kommen von fünf Stellen, jedes eine JSON-Datei im selben Format:\n\n"
         "- die Richtlinie des Administrators, Schlüssel `rules`;\n"
         "- `~/.config/sandbox-ai/rules.json`, bei jedem Start;\n"
         "- `--rules DATEI`, für einen Start (außerhalb jedes Arbeitsordners);\n"
         "- `--allow-http 'HOST METHODEN PFAD'`;\n"
         "- die vertraute Projektdatei, Schlüssel `rules`.\n\n"
         "Jedes Regelwerk entscheidet für sich, und die strengste Entscheidung gewinnt: deny vor ask vor allow. "
         "Keine Regel öffnet die Firewall. Durchgesetzt werden die Regeln vom Verbindungs-Proxy und vom Relay vor "
         "jedem MCP-Server, die der Agent nicht erreicht; Regeln für die eigenen Werkzeuge des Agenten laufen über "
         "seine Hooks, eine Leitplanke, keine Grenze. `--dry-run` mit `--rules` listet die Regelwerke und stoppt "
         "bei Fehlern. Auf der Tafel sind Regelwerke ein eigenes Teil.\n\n"
         "Ein kurzes Beispiel, `~/.config/sandbox-ai/github.json`: `--allow-github` öffnet jede Anfrage an "
         "api.github.com; dies hält Gists und Schreiben in Issues fern und fragt vor einem Release:\n\n"
         "```json\n"
         "{\"version\": 1, \"http\": [\n"
         "  {\"id\": \"no-gists\", \"host\": \"api.github.com\", \"path\": \"/gists/**\", \"decision\": \"deny\"},\n"
         "  {\"id\": \"no-issue-writes\", \"host\": \"api.github.com\", \"methods\": [\"POST\", \"PATCH\", \"PUT\", "
         "\"DELETE\"], \"path\": \"/repos/*/*/issues/**\", \"decision\": \"deny\"},\n"
         "  {\"id\": \"releases\", \"host\": \"api.github.com\", \"methods\": \"POST\", \"path\": "
         "\"/repos/*/*/releases\", \"decision\": \"ask\"}]}\n"
         "```\n\n"
         "In der App hat das Feld der Regelwerk-Dateien (das Teil Freigaben und Regeln auf der Tafel, "
         "Regelwerk-Datei auf der Seite Start) Neu aus Beispiel…, das diesen Anfang (und ein MCP-Beispiel) in eine "
         "Datei schreibt, die Sie außerhalb jedes Arbeitsordners wählen, und Prüfen, das den Launcher fragt, ob er "
         "die Dateien liest."),
        cli=("--rules FILE", "--allow-http", "--dry-run"),
        see=("taint", "approvals", "proxy", "mcp", "policy"),
        tours=()),
    section(
        "mcp", "concepts", ("Local MCP servers", "Lokale MCP-Server"),
        ("Local MCP servers run in a sandbox of their own, without credentials and, unless their definition says "
         "otherwise, without network. `--mcp NAME` starts the server sandbox if it is not running yet and "
         "connects the agent; the server keeps running for other agents.\n\n"
         "- There is no network path between the sandboxes: the agent talks to the server over a Unix socket "
         "through a small bridge.\n"
         "- Define servers in `~/.config/sandbox-ai/mcp.json` or under `mcp_servers` in a trusted project file. "
         "The built-in `demo` server shows that a server runs confined.\n"
         "- Catalogs: sandbox-ai-mcp (search and web) and axross-mcp-hub (security tools) offer audited servers "
         "pinned by digest. `sandbox-ai mcp add` takes an entry only from a signed catalog, unless you say "
         "`--unsigned`; `sandbox-ai mcp list` names the catalog each server came from.\n"
         "- The relay in front of every server is a gateway: `mcp` rules in a ruleset allow, ask or deny each "
         "tool call, and every call is logged in `mcp-calls.jsonl`.\n\n"
         "The agent decides what a server does: a fetch or browser server with network sends whatever the agent "
         "asks it to. A server with network can get a VPN of its own (`--vpn`).",
         "Lokale MCP-Server laufen in einer eigenen Sandbox, ohne Anmeldedaten und, wenn ihre Definition nichts "
         "anderes sagt, ohne Netzwerk. `--mcp NAME` startet die Server-Sandbox, falls sie noch nicht läuft, und "
         "verbindet den Agenten; der Server läuft für andere Agenten weiter.\n\n"
         "- Zwischen den Sandboxen gibt es keinen Netzwerkweg: Der Agent spricht über einen Unix-Socket und eine "
         "kleine Brücke mit dem Server.\n"
         "- Definieren Sie Server in `~/.config/sandbox-ai/mcp.json` oder unter `mcp_servers` einer vertrauten "
         "Projektdatei. Der eingebaute Server `demo` zeigt, dass ein Server eingesperrt läuft.\n"
         "- Kataloge: sandbox-ai-mcp (Suche und Web) und axross-mcp-hub (Sicherheitswerkzeuge) bieten geprüfte, "
         "per Digest festgelegte Server. `sandbox-ai mcp add` nimmt einen Eintrag nur aus einem signierten "
         "Katalog, außer Sie sagen `--unsigned`; `sandbox-ai mcp list` nennt den Katalog, aus dem jeder Server "
         "kam.\n"
         "- Das Relay vor jedem Server ist ein Gateway: `mcp`-Regeln eines Regelwerks erlauben, erfragen oder "
         "verbieten jeden Aufruf, und jeder Aufruf wird in `mcp-calls.jsonl` protokolliert.\n\n"
         "Der Agent bestimmt, was ein Server tut: Ein Fetch- oder Browser-Server mit Netzwerk schickt, was der "
         "Agent verlangt. Ein Server mit Netzwerk kann ein eigenes VPN bekommen (`--vpn`)."),
        cli=("--mcp NAME", "sandbox-ai mcp list", "sandbox-ai mcp catalog", "sandbox-ai mcp add NAME",
             "sandbox-ai mcp start NAME", "sandbox-ai mcp stop NAME"),
        see=("page-mcp", "rulesets", "vpn-tor", "teams"),
        tours=()),
    section(
        "devices", "concepts", ("Devices and GPUs", "Geräte und GPUs"),
        ("Out of the box a sandbox sees no host hardware. There are three ways to give the agent some, the "
         "safest first:\n\n"
         "- A service on the host: the device stays on the host, a program there offers it over TCP (ser2net, "
         "openocd, adb, a model server), and the sandbox reaches that port with `--allow-host-port PORT`.\n"
         "- `--device PATH`: the device file itself, such as a serial port, `/dev/kvm` or `usb:VID:PID`. Block "
         "devices, consoles and input devices are always refused.\n"
         "- `--gpu nvidia|dri|rocm`: all GPU devices in one option. NVIDIA needs CDI; Intel and AMD need GPU "
         "libraries in your own image.\n\n"
         "`sandbox-ai devices add` and `remove` change the devices of a running sandbox; the Sandboxes & sessions "
         "page does the same with Add device… and Remove device. Device rules in a ruleset can allow, ask or "
         "refuse each device by USB IDs, serial or class. Every device is kernel code on your host that the agent "
         "can call past both sandbox layers: pass only what the task needs. Bluetooth does not work through "
         "`--device`.",
         "Von sich aus sieht eine Sandbox keine Hardware des Hosts. Es gibt drei Wege, dem Agenten welche zu "
         "geben, der sicherste zuerst:\n\n"
         "- Ein Dienst auf dem Host: Das Gerät bleibt auf dem Host, ein Programm dort bietet es über TCP an "
         "(ser2net, openocd, adb, ein Modellserver), und die Sandbox erreicht diesen Port mit `--allow-host-port "
         "PORT`.\n"
         "- `--device PFAD`: die Gerätedatei selbst, etwa ein serieller Port, `/dev/kvm` oder `usb:VID:PID`. "
         "Blockgeräte, Konsolen und Eingabegeräte werden immer abgelehnt.\n"
         "- `--gpu nvidia|dri|rocm`: alle GPU-Geräte mit einer Option. NVIDIA braucht CDI; Intel und AMD brauchen "
         "GPU-Bibliotheken in einem eigenen Image.\n\n"
         "`sandbox-ai devices add` und `remove` ändern die Geräte einer laufenden Sandbox; die Seite Sandboxen & "
         "Sitzungen tut dasselbe mit Gerät hinzufügen… und Gerät entfernen. Geräteregeln eines Regelwerks können "
         "jedes Gerät nach USB-IDs, Seriennummer oder Klasse erlauben, erfragen oder ablehnen. Jedes Gerät ist "
         "Kernel-Code auf Ihrem Host, den der Agent an beiden Sandbox-Schichten vorbei aufrufen kann: Geben Sie "
         "nur, was die Aufgabe braucht. Bluetooth funktioniert nicht über `--device`."),
        cli=("--device", "--gpu", "--allow-host-port", "sandbox-ai devices list", "sandbox-ai devices add",
             "sandbox-ai devices remove"),
        see=("page-sandboxes", "rulesets", "security-modes", "llm"),
        tours=()),
    section(
        "display", "concepts", ("GUI apps and displays", "GUI-Programme und Anzeigen"),
        ("A sandbox has no display unless you give it one.\n\n"
         "- `--gui` starts a virtual X display (Xvfb) and sets `DISPLAY`; `--gui-screen WxHxD` sets its size.\n"
         "- `--gui-vnc DIR` adds VNC on the Unix socket `DIR/vnc.sock`, without TCP. View it with a client that "
         "speaks Unix sockets, such as TigerVNC's `vncviewer DIR/vnc.sock`.\n"
         "- `--gui-wayland` exposes your Wayland socket, so windows appear on your desktop. It gives the agent "
         "access to your compositor and prints a warning.\n"
         "- `--audio virtual` starts a sound server with a null sink inside the sandbox; nothing reaches your "
         "speakers or microphones.\n\n"
         "X11 passthrough is deliberately not offered: X11 exposes the keystrokes and screen of every host window. "
         "Browsers are not in the images; Playwright fetches them, which needs network. The VNC folder must lie "
         "outside the workspaces. The security mode `high` refuses `--gui-wayland`, `paranoid` also `--gui`.",
         "Eine Sandbox hat keine Anzeige, solange Sie ihr keine geben.\n\n"
         "- `--gui` startet eine virtuelle X-Anzeige (Xvfb) und setzt `DISPLAY`; `--gui-screen BxHxT` setzt ihre "
         "Größe.\n"
         "- `--gui-vnc DIR` fügt VNC auf dem Unix-Socket `DIR/vnc.sock` hinzu, ohne TCP. Sehen Sie sie mit einem "
         "Client an, der Unix-Sockets spricht, etwa `vncviewer DIR/vnc.sock` von TigerVNC.\n"
         "- `--gui-wayland` gibt Ihren Wayland-Socket frei, sodass Fenster auf Ihrem Desktop erscheinen. Das gibt "
         "dem Agenten Zugriff auf Ihren Compositor und erzeugt eine Warnung.\n"
         "- `--audio virtual` startet in der Sandbox einen Soundserver mit Null-Sink; nichts erreicht Ihre "
         "Lautsprecher oder Mikrofone.\n\n"
         "X11-Durchreichung gibt es absichtlich nicht: X11 gibt Tastendrücke und Bildschirm jedes Fensters des "
         "Hosts preis. Browser sind nicht in den Images; Playwright lädt sie, dafür braucht es Netzwerk. Der "
         "VNC-Ordner muss außerhalb der Arbeitsordner liegen. Der Sicherheitsmodus `high` lehnt `--gui-wayland` ab, "
         "`paranoid` auch `--gui`."),
        cli=("--gui", "--gui-screen", "--gui-vnc DIR", "--gui-wayland", "--audio virtual"),
        see=("security-modes", "page-images"),
        tours=()),
    section(
        "vpn-tor", "concepts", ("VPN profiles and Tor", "VPN-Profile und Tor"),
        ("A VPN can sit in front of a sandbox (Podman backend). The tunnel runs in a container of its own in the "
         "sandbox's network, a kill switch lets nothing else out (IPv4 and IPv6), and before the agent starts the "
         "addresses and DNS seen from outside are checked.\n\n"
         "- Profiles: WireGuard (any wg-quick file), OpenVPN, Mullvad, Tailscale or Headscale, and Tor. Add them "
         "with `sandbox-ai vpn add` or on the Credentials page (WireGuard, OpenVPN, Mullvad, Tailscale); Tor "
         "profiles with `sandbox-ai vpn add NAME --tor`.\n"
         "- Use one with `--vpn NAME`. `sandbox-ai vpn check NAME` brings it up alone, checks it and takes it "
         "down. A failed check stops the start; `--vpn-check warn` only warns.\n"
         "- Keys and passwords live in `sandbox-ai secrets`; only the VPN container gets them.\n\n"
         "Tor sends every TCP connection through Tor, the agent's own API calls included; it is TCP and IPv4 "
         "only, slower, and many sites refuse Tor exits. Build the VPN image once with `sandbox-ai build vpn`. "
         "Bubblewrap is not supported. An MCP server has a network of its own and needs its own `--vpn`.",
         "Ein VPN kann vor einer Sandbox sitzen (Podman-Backend). Der Tunnel läuft in einem eigenen Container im "
         "Netzwerk der Sandbox, ein Kill-Switch lässt nichts anderes hinaus (IPv4 und IPv6), und bevor der Agent "
         "startet, werden die von außen gesehenen Adressen und das DNS geprüft.\n\n"
         "- Profile: WireGuard (jede wg-quick-Datei), OpenVPN, Mullvad, Tailscale oder Headscale und Tor. Legen "
         "Sie sie mit `sandbox-ai vpn add` oder auf der Seite Zugangsdaten an (WireGuard, OpenVPN, Mullvad, "
         "Tailscale); Tor-Profile mit `sandbox-ai vpn add NAME --tor`.\n"
         "- Nutzen Sie eines mit `--vpn NAME`. `sandbox-ai vpn check NAME` bringt es allein hoch, prüft es und "
         "baut es wieder ab. Eine fehlgeschlagene Prüfung stoppt den Start; `--vpn-check warn` warnt nur.\n"
         "- Schlüssel und Passwörter liegen in `sandbox-ai secrets`; nur der VPN-Container bekommt sie.\n\n"
         "Tor leitet jede TCP-Verbindung durch Tor, auch die API-Aufrufe des Agenten; nur TCP und IPv4, "
         "langsamer, und viele Seiten weisen Tor-Ausgänge ab. Bauen Sie das VPN-Image einmal mit `sandbox-ai "
         "build vpn`. Bubblewrap wird nicht unterstützt. Ein MCP-Server hat ein eigenes Netzwerk und braucht ein "
         "eigenes `--vpn`."),
        cli=("sandbox-ai build vpn", "sandbox-ai vpn add", "sandbox-ai vpn list", "sandbox-ai vpn check NAME",
             "sandbox-ai vpn remove NAME", "--vpn", "--vpn-check"),
        see=("network", "page-credentials", "secrets", "mcp", "llm"),
        tours=()),
    section(
        "llm", "concepts", ("LLM servers", "LLM-Server"),
        ("Every agent can work with a model on your own server instead of the cloud: Ollama, LM Studio, the "
         "llama.cpp server, vLLM or any other OpenAI-compatible server, on this machine, in your LAN, behind a VPN "
         "or on the internet.\n\n"
         "- Register it once by name with `sandbox-ai llm add NAME --kind KIND --url URL`; its key stays in "
         "`sandbox-ai secrets`.\n"
         "- `sandbox-ai llm models NAME` lists what it serves, `sandbox-ai llm check NAME --agent AGENT` tests "
         "address, TLS, key, model and the API the agent needs. `sandbox-ai llm status [NAME]` says what each "
         "server has loaded and uses now (VRAM/RAM, KV cache, requests: what its API reports; `--watch SECONDS` "
         "reads again).\n"
         "- Start with `--llm NAME[:MODEL]`: it opens exactly that server, configures the agent for it and hands "
         "the agent a placeholder instead of the key. Add `--no-provider-defaults` to reach nothing else.\n"
         "- A server registered with `--vpn PROFILE` brings its VPN along.\n\n"
         "Claude Code speaks only the Anthropic Messages API, so a generic OpenAI-compatible server works with "
         "Codex and OpenCode, not Claude. Plain `http` goes only to this host's loopback and to private "
         "networks; anything else needs `llm add --insecure-http` and is warned about at every start.",
         "Jeder Agent kann mit einem Modell auf Ihrem eigenen Server statt in der Cloud arbeiten: Ollama, "
         "LM Studio, der Server von llama.cpp, vLLM oder jeder andere OpenAI-kompatible Server, auf diesem "
         "Rechner, im LAN, hinter einem VPN oder im Internet.\n\n"
         "- Legen Sie ihn einmal unter einem Namen an mit `sandbox-ai llm add NAME --kind ART --url URL`; sein "
         "Schlüssel bleibt in `sandbox-ai secrets`.\n"
         "- `sandbox-ai llm models NAME` listet, was er anbietet, `sandbox-ai llm check NAME --agent AGENT` prüft "
         "Adresse, TLS, Schlüssel, Modell und die API, die der Agent braucht. `sandbox-ai llm status [NAME]` sagt, "
         "was jeder Server jetzt geladen hat und belegt (VRAM/RAM, KV-Cache, Anfragen: was seine API meldet; "
         "`--watch SEKUNDEN` liest erneut).\n"
         "- Starten Sie mit `--llm NAME[:MODELL]`: Das öffnet genau diesen Server, richtet den Agenten dafür ein "
         "und gibt ihm einen Platzhalter statt des Schlüssels. Mit `--no-provider-defaults` erreicht er sonst "
         "nichts.\n"
         "- Ein mit `--vpn PROFIL` angelegter Server bringt sein VPN mit.\n\n"
         "Claude Code spricht nur die Anthropic-Messages-API; ein allgemeiner OpenAI-kompatibler Server "
         "funktioniert daher mit Codex und OpenCode, nicht mit Claude. Reines `http` geht nur an das Loopback "
         "dieses Hosts und an private Netze; alles andere braucht `llm add --insecure-http` und wird bei jedem "
         "Start angemahnt."),
        cli=("sandbox-ai llm add NAME", "sandbox-ai llm list", "sandbox-ai llm models NAME",
             "sandbox-ai llm check NAME", "sandbox-ai llm remove NAME", "--llm", "--no-provider-defaults"),
        see=("page-llm", "vpn-tor", "network", "secrets", "app-presets-gallery"),
        tours=("area:llm",)),
    section(
        "web", "concepts", ("Web interface", "Weboberfläche"),
        ("`sandbox-ai web` is the desktop app's counterpart in the browser, with the same themes. One service on "
         "one machine manages it and every host of `sandbox-ai hosts` over SSH: starts and sessions with their "
         "terminals, approvals, the doctor, the logs, LLM servers and each host's defaults.\n\n"
         "- `sandbox-ai web user add NAME` creates a login with a password (12 or more characters) and a TOTP "
         "app.\n"
         "- `sandbox-ai web vendor` fetches xterm.js for the terminals, pinned and checked.\n"
         "- `sandbox-ai web serve` runs it on `https://localhost:8443/` and prints its certificate's "
         "fingerprint.\n\n"
         "It serves HTTPS only, on `127.0.0.1` unless `--listen` says otherwise; to reach it from elsewhere, "
         "prefer an SSH tunnel. Every action is one sandbox-ai command, each argument quoted on its own. An "
         "audit log records logins, failures, changes and terminals. No sandbox can reach the service's port.",
         "`sandbox-ai web` ist das Gegenstück der Desktop-App im Browser, mit denselben Farbschemata. Ein Dienst "
         "auf einem Rechner verwaltet ihn und jeden Host von `sandbox-ai hosts` über SSH: Starts und Sitzungen mit "
         "ihren Terminals, Freigaben, den doctor, die Protokolle, LLM-Server und die Vorgaben jedes Hosts.\n\n"
         "- `sandbox-ai web user add NAME` legt eine Anmeldung mit Passwort (12 oder mehr Zeichen) und einer "
         "TOTP-App an.\n"
         "- `sandbox-ai web vendor` lädt xterm.js für die Terminals, festgelegt und geprüft.\n"
         "- `sandbox-ai web serve` startet ihn auf `https://localhost:8443/` und zeigt den Fingerabdruck seines "
         "Zertifikats.\n\n"
         "Er spricht nur HTTPS, auf `127.0.0.1`, solange `--listen` nichts anderes sagt; um ihn von anderswo zu "
         "erreichen, nehmen Sie besser einen SSH-Tunnel. Jede Aktion ist ein sandbox-ai-Befehl, jedes Argument "
         "einzeln gequotet. Ein Audit-Log hält Anmeldungen, Fehlschläge, Änderungen und Terminals fest. Keine "
         "Sandbox erreicht den Port des Dienstes."),
        cli=("sandbox-ai web user add NAME", "sandbox-ai web vendor", "sandbox-ai web serve",
             "sandbox-ai web serve --listen ADDRESS:PORT", "sandbox-ai defaults show"),
        see=("hosts", "page-hosts"),
        tours=()),
    section(
        "hosts", "concepts", ("Other hosts", "Andere Hosts"),
        ("`sandbox-ai hosts` manages other machines, or Proxmox LXC containers, whose sandbox-ai this one "
         "controls over SSH. The desktop app's Hosts page and the web interface use the same list, kept in "
         "`~/.config/sandbox-ai/hosts.json` next to `local` (this machine).\n\n"
         "- `sandbox-ai hosts add NAME USER@HOST` adds one; `--identity FILE` picks a key, `--port N` a port.\n"
         "- `sandbox-ai hosts test NAME` checks SSH, the host's sandbox-ai version and its doctor.\n"
         "- `sandbox-ai hosts run NAME -- ARGS` runs a sandbox-ai command there, `-- attach NAME` a terminal into "
         "a session.\n\n"
         "SSH runs in batch mode, so it never hangs on a password prompt. The host key is learnt at the first "
         "contact and checked strictly after that. Nothing listens on the network on the hosts: they need sshd and "
         "sandbox-ai only. A non-interactive login reads no `~/.bashrc`, so the command falls back to "
         "`~/.local/bin/sandbox-ai`.",
         "`sandbox-ai hosts` verwaltet andere Rechner oder Proxmox-LXC-Container, deren sandbox-ai dieser über SSH "
         "steuert. Die Seite Hosts der Desktop-App und die Weboberfläche nutzen dieselbe Liste, abgelegt in "
         "`~/.config/sandbox-ai/hosts.json` neben `local` (diesem Rechner).\n\n"
         "- `sandbox-ai hosts add NAME BENUTZER@HOST` fügt einen hinzu; `--identity DATEI` wählt einen Schlüssel, "
         "`--port N` einen Port.\n"
         "- `sandbox-ai hosts test NAME` prüft SSH, die sandbox-ai-Version des Hosts und seinen doctor.\n"
         "- `sandbox-ai hosts run NAME -- ARGS` führt dort einen sandbox-ai-Befehl aus, `-- attach NAME` ein "
         "Terminal in eine Sitzung.\n\n"
         "SSH läuft im Batch-Modus und hängt daher nie an einer Passwortabfrage. Der Host-Schlüssel wird beim "
         "ersten Kontakt gelernt und danach streng geprüft. Auf den Hosts lauscht nichts im Netz: Sie brauchen "
         "nur sshd und sandbox-ai. Eine nicht interaktive Anmeldung liest keine `~/.bashrc`, daher greift der "
         "Befehl auf `~/.local/bin/sandbox-ai` zurück."),
        cli=("sandbox-ai hosts list", "sandbox-ai hosts add NAME USER@HOST", "sandbox-ai hosts test NAME",
             "sandbox-ai hosts run NAME -- ARGS", "sandbox-ai hosts remove NAME"),
        see=("page-hosts", "web"),
        tours=()),
    section(
        "teams", "concepts", ("Agent teams", "Agenten-Teams"),
        ("`sandbox-ai team` starts one sandbox per agent on a shared workspace, and they work together through a "
         "task board.\n\n"
         "- `sandbox-ai team start NAME --agents claude,codex,opencode -w DIR` starts the team; options after "
         "`--` hold for every member. The first agent leads.\n"
         "- Each member is a named session (`NAME-claude`, `NAME-codex`, ...): attach, pause, resume or stop it "
         "like any start.\n"
         "- `team show` lists members, their state and the board; `team post NAME AGENT TASK` gives a member a "
         "task from you; `team stop` ends every member and the board, `team delete` also removes their HOMEs.\n\n"
         "The board is an MCP server in a sandbox of its own, without network; every member reaches it through "
         "its relay, so `mcp` rules and questions hold. The members share the workspace: nothing stops two "
         "agents from changing the same file. For a single helper, `--helper AGENT` gives an agent another one "
         "to hand tasks to, and `--with AGENT` puts another agent into the same sandbox.",
         "`sandbox-ai team` startet eine Sandbox je Agent auf einem gemeinsamen Arbeitsordner, und sie arbeiten über "
         "eine Aufgabentafel zusammen.\n\n"
         "- `sandbox-ai team start NAME --agents claude,codex,opencode -w DIR` startet das Team; Optionen nach "
         "`--` gelten für jedes Mitglied. Der erste Agent leitet.\n"
         "- Jedes Mitglied ist eine benannte Sitzung (`NAME-claude`, `NAME-codex`, ...): Verbinden, pausieren, "
         "fortsetzen oder stoppen Sie es wie jeden Start.\n"
         "- `team show` listet Mitglieder, ihren Zustand und die Tafel; `team post NAME AGENT AUFGABE` gibt einem "
         "Mitglied eine Aufgabe von Ihnen; `team stop` beendet alle Mitglieder und die Tafel, `team delete` "
         "entfernt auch ihre HOMEs.\n\n"
         "Die Aufgabentafel ist ein MCP-Server in einer eigenen Sandbox, ohne Netzwerk; jedes Mitglied erreicht "
         "sie über sein Relay, daher gelten `mcp`-Regeln und Fragen. Die Mitglieder teilen den Arbeitsordner: Nichts "
         "hindert zwei Agenten daran, dieselbe Datei zu ändern. Für einen einzelnen Helfer gibt `--helper AGENT` "
         "einem Agenten einen weiteren, an den er Aufgaben abgibt, und `--with AGENT` holt einen weiteren Agenten "
         "in dieselbe Sandbox."),
        cli=("sandbox-ai team start NAME --agents A,B", "sandbox-ai team list", "sandbox-ai team show NAME",
             "sandbox-ai team post NAME AGENT TASK", "sandbox-ai team stop NAME", "sandbox-ai team delete NAME",
             "--helper", "--with"),
        see=("sessions", "mcp", "rulesets"),
        tours=()),
    section(
        "memory-watch", "concepts", ("Memory watch", "Speicherwächter"),
        ("When a sandbox's memory limit is full, the kernel ends a process, and if that is the sandbox's "
         "supervisor the whole run ends. So the launcher watches the sandbox's memory from the host while it "
         "runs; the sandbox cannot see the watch.\n\n"
         "- At 85 % it tells you (a desktop notification, and `memory.jsonl` in the log directory) and the agent: "
         "`/sandbox/session/memory.json` gives used and limit, what the files in `/tmp` and HOME take, and "
         "advice.\n"
         "- At 95 % it ends the process with the most memory that is neither sandbox-ai's own nor the agent "
         "itself, typically a test run or a build: the agent sees its command fail and the run goes on.\n"
         "- After the run it says what it ended, and whether the kernel ended processes anyway.\n\n"
         "Files in `/tmp` and `/var/tmp` live in memory and count against the limit; each holds at most half of "
         "it. If the watch had to step in, start with a larger `--memory` or run fewer things at once. The watch "
         "needs a local engine on Linux, not `--podman-url`.",
         "Ist das Speicherlimit einer Sandbox voll, beendet der Kernel einen Prozess, und ist das der Supervisor "
         "der Sandbox, endet der ganze Lauf. Deshalb beobachtet der Launcher den Speicher der Sandbox vom Host "
         "aus, solange sie läuft; die Sandbox sieht den Wächter nicht.\n\n"
         "- Bei 85 % sagt er es Ihnen (Desktop-Benachrichtigung und `memory.jsonl` im Protokollverzeichnis) und dem "
         "Agenten: `/sandbox/session/memory.json` nennt Verbrauch und Limit, was die Dateien in `/tmp` und HOME "
         "belegen, und einen Rat.\n"
         "- Bei 95 % beendet er den Prozess mit dem meisten Speicher, der weder zu sandbox-ai gehört noch der "
         "Agent selbst ist, meist einen Testlauf oder Build: Der Agent sieht seinen Befehl scheitern, und der "
         "Lauf geht weiter.\n"
         "- Nach dem Lauf sagt er, was er beendet hat und ob der Kernel trotzdem Prozesse beendet hat.\n\n"
         "Dateien in `/tmp` und `/var/tmp` liegen im Speicher und zählen zum Limit; jedes fasst höchstens die "
         "Hälfte davon. Musste der Wächter eingreifen, starten Sie mit größerem `--memory` oder lassen Sie weniger "
         "gleichzeitig laufen. Der Wächter braucht eine lokale Engine unter Linux, nicht `--podman-url`."),
        cli=("--memory", "sandbox-ai limits RUN --memory SIZE"),
        see=("limits", "logs"),
        tours=()),
    section(
        "privacy-check", "concepts", ("Privacy check before publishing", "Datenschutzprüfung vor dem Veröffentlichen"),
        ("An agent that may push can publish what was never meant to leave your machine: your home path, the "
         "address of your LAN server, an account name, a token. sandbox-ai looks for these before they are "
         "published, in three places.\n\n"
         "- After every run, for each writable workspace that is a Git repository with a public remote, it shows "
         "what the next push would publish and should stay private.\n"
         "- In the sandbox, Git hooks warn at commit and refuse a push to a public remote with blocking findings; "
         "the connection proxy also holds every push over HTTP(S) until the check has seen it.\n"
         "- By hand: `sandbox-ai privacy-check PATH`, also for repositories you push from the host. The System "
         "page offers it as Privacy check of a repository….\n\n"
         "The rules are built in (keys and tokens), derived from the run (your HOME path, private addresses, your "
         "tokens), and your own files in `~/.config/sandbox-ai/privacy`. `--privacy-check always` checks every "
         "writable workspace, `off` switches the check off. It stops accidents, not a determined agent: encoded "
         "data passes any pattern.",
         "Ein Agent, der pushen darf, kann veröffentlichen, was Ihren Rechner nie verlassen sollte: Ihren "
         "HOME-Pfad, die Adresse Ihres LAN-Servers, einen Kontonamen, ein Token. sandbox-ai sucht danach, bevor es "
         "veröffentlicht wird, an drei Stellen.\n\n"
         "- Nach jedem Lauf zeigt es für jeden beschreibbaren Arbeitsordner, der ein Git-Repository mit öffentlichem "
         "Remote ist, was der nächste Push veröffentlichen würde und privat bleiben sollte.\n"
         "- In der Sandbox warnen Git-Hooks beim Commit und weisen einen Push an ein öffentliches Remote mit "
         "blockierenden Funden ab; der Verbindungs-Proxy hält zudem jeden Push über HTTP(S) an, bis die Prüfung "
         "ihn gesehen hat.\n"
         "- Von Hand: `sandbox-ai privacy-check PFAD`, auch für Repositorys, aus denen Sie vom Host pushen. Die "
         "Seite System bietet das als Datenschutzprüfung eines Repositorys….\n\n"
         "Die Regeln sind eingebaut (Schlüssel und Tokens), aus dem Lauf abgeleitet (Ihr HOME-Pfad, private "
         "Adressen, Ihre Tokens) und Ihre eigenen Dateien in `~/.config/sandbox-ai/privacy`. `--privacy-check "
         "always` prüft jeden beschreibbaren Arbeitsordner, `off` schaltet die Prüfung ab. Sie verhindert Versehen, "
         "keinen entschlossenen Agenten: Kodierte Daten passieren jedes Muster."),
        cli=("sandbox-ai privacy-check PATH", "sandbox-ai privacy-check --all", "--privacy-check public|always|off",
             "--privacy-rules DIR"),
        see=("proxy", "secrets", "page-system", "security-modes"),
        tours=()),
    section(
        "secrets", "concepts", ("Secrets, logins and identities", "Geheimnisse, Logins und Identitäten"),
        ("`sandbox-ai secrets` keeps API keys and tokens in your system keyring, or in private files when there "
         "is none. A value is read from standard input, never from an argument, and never printed. A sandbox gets "
         "a secret only when a start names it:\n\n"
         "- `--secret-env NAME`: an environment variable the agent can read.\n"
         "- `--api-key HOST=NAME`: the agent gets a placeholder; the proxy puts the key in its place, only in a "
         "credential header of HTTPS requests to HOST.\n"
         "- `--git-token-secret URL=SECRET` gives Git a token; with `--token-proxy` it stays with the proxy.\n\n"
         "Logins that stay outside every sandbox: `sandbox-ai git-logins` adds a token to Git's requests to one "
         "server; `sandbox-ai publish-logins` does the same for npm, PyPI, crates.io, GitLab and GitHub, used only "
         "with `--publish`. `sandbox-ai identities` groups a commit name and address with Git and publish logins; "
         "`--identity NAME` chooses one. Agent logins are separate: `sandbox-ai login AGENT` gives sandboxes a "
         "login of their own, kept in a login store. A preset or project file only ever names a secret.",
         "`sandbox-ai secrets` bewahrt API-Schlüssel und Tokens in Ihrem Schlüsselbund auf, oder in privaten "
         "Dateien, wenn es keinen gibt. Ein Wert wird von der Standardeingabe gelesen, nie aus einem Argument, und "
         "nie ausgegeben. Eine Sandbox bekommt ein Geheimnis nur, wenn ein Start es nennt:\n\n"
         "- `--secret-env NAME`: eine Umgebungsvariable, die der Agent lesen kann.\n"
         "- `--api-key HOST=NAME`: Der Agent bekommt einen Platzhalter; der Proxy setzt den Schlüssel an seine "
         "Stelle, nur in einem Anmelde-Header von HTTPS-Anfragen an HOST.\n"
         "- `--git-token-secret URL=GEHEIMNIS` gibt Git ein Token; mit `--token-proxy` bleibt es beim Proxy.\n\n"
         "Logins, die außerhalb jeder Sandbox bleiben: `sandbox-ai git-logins` setzt ein Token in Gits Anfragen "
         "an einen Server ein; `sandbox-ai publish-logins` tut das für npm, PyPI, crates.io, GitLab und GitHub, "
         "nur mit `--publish` genutzt. `sandbox-ai identities` fasst Commit-Name und Adresse mit Git- und "
         "Publish-Logins zusammen; `--identity NAME` wählt eine. Logins der Agenten sind getrennt davon: "
         "`sandbox-ai login AGENT` gibt Sandboxen einen eigenen Login, im Login-Speicher aufbewahrt. Eine Vorlage "
         "oder Projektdatei nennt ein Geheimnis immer nur."),
        cli=("sandbox-ai secrets list", "sandbox-ai secrets set NAME", "sandbox-ai secrets delete NAME",
             "sandbox-ai git-logins add URL --secret NAME", "sandbox-ai publish-logins add KIND --secret NAME",
             "sandbox-ai identities add NAME", "sandbox-ai login AGENT", "--secret-env", "--api-key",
             "--git-token-secret", "--token-proxy", "--publish", "--identity"),
        see=("page-credentials", "proxy", "privacy-check", "vpn-tor"),
        tours=()),
    section(
        "logs", "concepts", ("Logs, activity and telemetry", "Protokolle, Aktivität und Telemetrie"),
        ("Every run with the connection proxy writes logs into a log directory: `--log-dir DIR`, else "
         "`~/.local/state/sandbox-ai/logs/PROJECT-HASH`. The directory is hidden from the agent.\n\n"
         "- The connection log: every request, push and refusal, credentials redacted.\n"
         "- `egress-denied.jsonl`: each dropped destination and each lookup of a name that is not allowed.\n"
         "- `processes.jsonl`: every process with its command line, redacted.\n"
         "- `files.jsonl` (only with `--log-dir`): reads, writes, creates and deletes under the workspaces, HOME, "
         "`/tmp` and the mounts.\n"
         "- `activity.jsonl`: the timeline of what the agents did.\n\n"
         "Each refusal names the process behind it. `--log-debug` adds the bodies of refused requests and more "
         "detail; credentials stay redacted. `sandbox-ai logs` summarizes a directory and suggests the option for "
         "each blocked target; every suggestion widens the sandbox. Telemetry of the agents to their makers is "
         "off by default (`--telemetry off`); `strict` allows nothing but the model's calls, `on` keeps the "
         "agents' own settings.",
         "Jeder Lauf mit Verbindungs-Proxy schreibt Protokolle in ein Protokollverzeichnis: `--log-dir DIR`, sonst "
         "`~/.local/state/sandbox-ai/logs/PROJEKT-HASH`. Das Verzeichnis ist vor dem Agenten verborgen.\n\n"
         "- Das Verbindungslog: jede Anfrage, jeder Push und jede Abweisung, Anmeldedaten geschwärzt.\n"
         "- `egress-denied.jsonl`: jedes verworfene Ziel und jede Auflösung eines nicht erlaubten Namens.\n"
         "- `processes.jsonl`: jeder Prozess mit seiner Befehlszeile, geschwärzt.\n"
         "- `files.jsonl` (nur mit `--log-dir`): Lesen, Schreiben, Anlegen und Löschen unter den Arbeitsordnern, HOME, "
         "`/tmp` und den Mounts.\n"
         "- `activity.jsonl`: die Zeitleiste dessen, was die Agenten taten.\n\n"
         "Jede Abweisung nennt den Prozess dahinter. `--log-debug` fügt die Bodies abgewiesener Anfragen und mehr "
         "Einzelheiten hinzu; Anmeldedaten bleiben geschwärzt. `sandbox-ai logs` fasst ein Verzeichnis zusammen "
         "und schlägt für jedes gesperrte Ziel die Option vor; jeder Vorschlag erweitert die Sandbox. Telemetrie "
         "der Agenten an ihre Hersteller ist standardmäßig aus (`--telemetry off`); `strict` erlaubt nur die "
         "Aufrufe des Modells, `on` behält die eigenen Einstellungen der Agenten."),
        cli=("--log-dir DIR", "--log-debug", "sandbox-ai logs", "sandbox-ai activity", "--telemetry off|strict|on"),
        see=("page-connections", "page-activity", "page-statistics", "proxy"),
        tours=()),
    section(
        "limits", "concepts", ("Resource limits", "Ressourcenlimits"),
        ("Every sandbox runs with limits on memory, CPU and processes:\n\n"
         "- `--memory SIZE`, default `8g`;\n"
         "- `--cpus N`, default half of the host's CPUs;\n"
         "- `--pids N`, default 4096.\n\n"
         "`none` removes a limit. `sandbox-ai limits RUN --memory 16g --cpus 4` changes them while the sandbox "
         "runs; the Sandboxes & sessions page does the same with Limits…, and the Board's Limits part can be "
         "applied live. Podman passes the limits to the container, Bubblewrap runs in a systemd scope. Both need "
         "a systemd user session with delegated cgroup controllers, as on every desktop; without one the sandbox "
         "starts without limits and says so. The security modes `high` and `paranoid` set smaller limits, and a "
         "larger one widens them.",
         "Jede Sandbox läuft mit Limits für Speicher, CPU und Prozesse:\n\n"
         "- `--memory GRÖSSE`, Vorgabe `8g`;\n"
         "- `--cpus N`, Vorgabe die Hälfte der CPUs des Hosts;\n"
         "- `--pids N`, Vorgabe 4096.\n\n"
         "`none` hebt ein Limit auf. `sandbox-ai limits RUN --memory 16g --cpus 4` ändert sie, während die "
         "Sandbox läuft; die Seite Sandboxen & Sitzungen tut dasselbe mit Grenzen…, und das Teil Limits der Tafel "
         "lässt sich sofort anwenden. Podman gibt die Limits an den Container, Bubblewrap läuft in einem "
         "systemd-Scope. Beide brauchen eine systemd-Benutzersitzung mit delegierten cgroup-Controllern, wie auf "
         "jedem Desktop; ohne sie startet die Sandbox ohne Limits und sagt es. Die Sicherheitsmodi `high` und "
         "`paranoid` setzen kleinere Limits, und ein größeres erweitert sie."),
        cli=("--memory", "--cpus", "--pids", "sandbox-ai limits RUN"),
        see=("memory-watch", "page-sandboxes", "security-modes"),
        tours=()),
    section(
        "policy", "concepts", ("Administrator policy", "Richtlinie des Administrators"),
        ("An administrator can fix what nobody on the machine may choose, in `/etc/sandbox-ai/policy.json`, owned "
         "and writable by root only. A broken or unsafe file stops every start.\n\n"
         "- `forbid`: options refused however they were chosen, also through a project file or a profile.\n"
         "- `backends`, `min_mode` (the laxest security mode), `max_approval_scope` (once, run or always).\n"
         "- `allow_within` and `allow_hosts`: a ceiling for addresses and named hosts.\n"
         "- `require_vpn`, `require_canaries`, `forbid_keep`, `require_signed_updates`, a default `subnet`, and "
         "`rules`, a ruleset that every run applies first.\n\n"
         "The app greys out everything the policy forbids, on the Start page, on the Board and in the wizard. "
         "System > Administrator policy shows it, and the Settings dialog shows it in full; only root can change "
         "it. The policy binds sandbox-ai as installed: it guards against convenience, project files and mistakes, "
         "not against a user who runs a modified copy.",
         "Ein Administrator kann festlegen, was niemand auf dem Rechner wählen darf, in "
         "`/etc/sandbox-ai/policy.json`, das nur root gehört und nur root schreiben kann. Eine kaputte oder "
         "unsichere Datei stoppt jeden Start.\n\n"
         "- `forbid`: Optionen, die abgelehnt werden, wie auch immer sie gewählt wurden, auch über eine "
         "Projektdatei oder ein Profil.\n"
         "- `backends`, `min_mode` (der laxeste Sicherheitsmodus), `max_approval_scope` (once, run oder always).\n"
         "- `allow_within` und `allow_hosts`: eine Obergrenze für Adressen und benannte Hosts.\n"
         "- `require_vpn`, `require_canaries`, `forbid_keep`, `require_signed_updates`, ein vorgegebenes `subnet` "
         "und `rules`, ein Regelwerk, das jeder Lauf zuerst anwendet.\n\n"
         "Die App graut alles aus, was die Richtlinie verbietet, auf der Seite Start, auf der Tafel und im "
         "Assistenten. System > Richtlinie des Administrators zeigt sie, der Dialog Einstellungen zeigt sie ganz; "
         "nur root kann sie ändern. Die Richtlinie bindet das installierte sandbox-ai: Sie schützt vor Bequemlichkeit, "
         "Projektdateien und Fehlern, nicht vor einem Benutzer, der eine veränderte Kopie ausführt."),
        cli=("sandbox-ai doctor",),
        see=("security-modes", "rulesets", "approvals", "page-system", "app-settings"),
        tours=()),
    section(
        "releases-update", "concepts", ("Install and update", "Installation und Updates"),
        ("sandbox-ai can be installed in four ways:\n\n"
         "- `install.sh`: the command line and the app, kept up to date (recommended). Update with `sandbox-ai "
         "update`.\n"
         "- `pip install sandbox-ai`: the command line in a Python environment; update with pip.\n"
         "- `sandbox-ai.pyz`: the command line in one file.\n"
         "- The AppImage (Linux) or `sandbox-ai.app` (macOS): the app with the command line inside.\n\n"
         "Updates through `install.sh` are signed: a release signs its `SHA256SUMS` with an SSH key, and once the "
         "installation trusts a key, an update is installed only with a good signature. Older releases are "
         "refused unless `--allow-downgrade`; the previous version is kept for a rollback. `sandbox-ai version "
         "--verbose` shows the trusted keys' fingerprints. The `.pyz`, the AppImage and the macOS app do not "
         "update themselves: download the new file.\n\n"
         "In the app, System > Check for updates runs `sandbox-ai update --check`, and Update sandbox-ai runs "
         "`sandbox-ai update`. Never install sandbox-ai into a project folder: an agent could rewrite it.",
         "sandbox-ai lässt sich auf vier Wegen installieren:\n\n"
         "- `install.sh`: Kommandozeile und App, aktuell gehalten (empfohlen). Aktualisieren mit `sandbox-ai "
         "update`.\n"
         "- `pip install sandbox-ai`: die Kommandozeile in einer Python-Umgebung; aktualisieren mit pip.\n"
         "- `sandbox-ai.pyz`: die Kommandozeile in einer Datei.\n"
         "- Das AppImage (Linux) oder `sandbox-ai.app` (macOS): die App mit der Kommandozeile darin.\n\n"
         "Updates über `install.sh` sind signiert: Ein Release signiert seine `SHA256SUMS` mit einem "
         "SSH-Schlüssel, und sobald die Installation einem Schlüssel vertraut, wird ein Update nur mit gültiger "
         "Signatur installiert. Ältere Releases werden abgelehnt, außer mit `--allow-downgrade`; die vorige "
         "Version bleibt für ein Zurück. `sandbox-ai version --verbose` zeigt die Fingerabdrücke der vertrauten "
         "Schlüssel. `.pyz`, AppImage und macOS-App aktualisieren sich nicht selbst: Laden Sie die neue Datei.\n\n"
         "In der App führt System > Auf Updates prüfen `sandbox-ai update --check` aus und sandbox-ai "
         "aktualisieren `sandbox-ai update`. Installieren Sie sandbox-ai nie in einen Projektordner: Ein Agent "
         "könnte es umschreiben."),
        cli=("sandbox-ai update --check", "sandbox-ai update", "sandbox-ai update --version X.Y.Z",
             "sandbox-ai version --verbose", "sandbox-ai doctor"),
        see=("page-system", "policy", "page-images"),
        tours=()),
)

BY_ID = {s.id: s for s in SECTIONS}
