"""Human labels of the launcher's options, in English and German (German as in sandbox-ai's README_DE).
The option itself (--workspace, -w) is shown next to its label; sandbox-ai's own help text (English) is
the tooltip. An option sandbox-ai adds later gets its option name as label until it is listed here."""
from __future__ import annotations

from . import i18n

OPTION_LABELS = {
    "--workspace": ("Workspaces", "Workspaces"),
    "--workspace-ro": ("Mount all workspaces read-only", "Alle Workspaces schreibgeschützt einbinden"),
    "--mount": ("Extra mounts", "Zusätzliche Mounts"),
    "--copy": ("Copy files into the sandbox HOME", "Dateien ins Sandbox-HOME kopieren"),
    "--state-dir": ("Persistent sandbox HOME", "Dauerhaftes Sandbox-HOME"),
    "--ask": ("Ask before unknown connections", "Bei unbekannten Verbindungen nachfragen"),
    "--profile": ("Network profiles", "Netzwerkprofile"),
    "--allow-host": ("Allowed host names (HTTPS)", "Erlaubte Hostnamen (HTTPS)"),
    "--allow-pypi": ("Allow PyPI", "PyPI erlauben"),
    "--allow-github": ("Allow GitHub", "GitHub erlauben"),
    "--allow-gitlab": ("Allow GitLab", "GitLab erlauben"),
    "--allow-kernel-org": ("Allow kernel.org", "kernel.org erlauben"),
    "--allow-duckduckgo": ("Allow DuckDuckGo", "DuckDuckGo erlauben"),
    "--allow-tcp": ("Allowed TCP ports (IP:PORT)", "Erlaubte TCP-Ports (IP:PORT)"),
    "--allow": ("Allowed addresses and networks", "Erlaubte Adressen und Netze"),
    "--allow-host-port": ("Ports on the host's loopback", "Ports am Loopback des Hosts"),
    "--frontier": ("Allow the OpenAI and Anthropic APIs", "APIs von OpenAI und Anthropic erlauben"),
    "--no-provider-defaults": ("Leave out the agent's default hosts", "Standard-Hosts des Agenten weglassen"),
    "--dns": ("Real DNS (1.1.1.1, 8.8.8.8)", "Echtes DNS (1.1.1.1, 8.8.8.8)"),
    "--nameserver": ("Own name servers", "Eigene Nameserver"),
    "--egress-all-allow": ("Allow every outbound destination", "Jedes ausgehende Ziel erlauben"),
    "--offline": ("No network at all", "Gar kein Netz"),
    "--network": ("Podman network", "Podman-Netz"),
    "--subnet": ("Sandbox subnet (IPv4)", "Subnetz der Sandbox (IPv4)"),
    "--rules": ("Ruleset file", "Regelwerk-Datei"),
    "--llm": ("LLM server", "LLM-Server"),
    "--log-debug": ("Debug details in the logs", "Debug-Details in den Logs"),
    "--subnet6": ("Sandbox subnet (IPv6)", "Subnetz der Sandbox (IPv6)"),
    "--ip": ("Fixed IPv4 address", "Feste IPv4-Adresse"),
    "--ip6": ("Fixed IPv6 address", "Feste IPv6-Adresse"),
    "--no-proxy": ("No connection proxy", "Kein Verbindungs-Proxy"),
    "--no-inspect": ("Pass TLS through unopened", "TLS ungeöffnet durchreichen"),
    "--privacy-check": ("Privacy check after the run", "Datenschutzprüfung nach dem Lauf"),
    "--privacy-rules": ("Own privacy rules", "Eigene Datenschutzregeln"),
    "--log-dir": ("Log folder", "Log-Verzeichnis"),
    "--git-identity": ("Commit as the host's Git user", "Als Git-Benutzer des Hosts committen"),
    "--git-token-env": ("Git token from a host variable", "Git-Token aus einer Host-Variable"),
    "--git-token-file": ("Git token from a file", "Git-Token aus einer Datei"),
    "--git-token-secret": ("Git token from a secret", "Git-Token aus einem Geheimnis"),
    "--git-credentials": ("Git logins from a credential file", "Git-Logins aus einer Credential-Datei"),
    "--dangerously-skip-permissions": ("Agent without approval prompts", "Agent ohne Rückfragen"),
    "--mcp": ("MCP servers", "MCP-Server"),
    "--kit": ("Additional kits", "Zusätzliche Kits"),
    "--kit-dir": ("Kit folders", "Kit-Verzeichnisse"),
    "--no-default-kit": ("Without the default kit", "Ohne das Standard-Kit"),
    "--no-memory": ("No project memory", "Kein Projekt-Memory"),
    "--no-plugins": ("No plugins", "Keine Plugins"),
    "--local-config": ("OpenCode: local configuration only", "OpenCode: rein lokale Konfiguration"),
    "--env": ("Host variables for the agent", "Host-Variablen für den Agenten"),
    "--secret-env": ("Secrets as variables", "Geheimnisse als Variablen"),
    "--copy-auth": ("Require a copy of the login", "Kopie der Anmeldedaten verlangen"),
    "--no-auth": ("Do not copy host logins", "Keine Anmeldedaten des Hosts kopieren"),
    "--auth-file": ("Login file to copy", "Anmeldedatei kopieren"),
    "--import-codex-auth": ("OpenCode: import a Codex login", "OpenCode: Codex-Login importieren"),
    "--gui": ("Virtual display (Xvfb)", "Virtuelles Display (Xvfb)"),
    "--gui-vnc": ("VNC socket folder", "VNC-Socket-Verzeichnis"),
    "--gui-screen": ("Virtual screen size", "Größe des virtuellen Bildschirms"),
    "--gui-wayland": ("Share the host's Wayland socket", "Wayland-Socket des Hosts freigeben"),
    "--tmux": ("tmux in the sandbox", "tmux in der Sandbox"),
    "--device": ("Host devices", "Geräte des Hosts"),
    "--gpu": ("GPU", "GPU"),
    "--memory": ("Memory limit", "Speicherlimit"),
    "--cpus": ("CPU limit", "CPU-Limit"),
    "--pids": ("Process limit", "Prozesslimit"),
    "--backend": ("Sandbox backend", "Sandbox-Backend"),
    "--image": ("Podman image", "Podman-Image"),
    "--image-variant": ("Image variant (sandbox-ai images)", "Image-Variante (sandbox-ai images)"),
    "--podman-url": ("Podman service URL", "URL des Podman-Dienstes"),
    "--binary": ("Agent program", "Agent-Programm"),
    "--runtime": ("Runtime folders (bwrap)", "Laufzeitverzeichnisse (bwrap)"),
    "--config": ("Project configuration file", "Projektkonfiguration"),
    "--no-config": ("Ignore .sandbox-ai.json", ".sandbox-ai.json ignorieren"),
    "--probe": ("Only check the isolation (no agent)", "Nur die Isolation prüfen (ohne Agent)"),
}
# Options that only widen or weaken the sandbox: shown with a warning colour when set.
DANGEROUS = {"--dangerously-skip-permissions", "--egress-all-allow", "--no-proxy", "--gui-wayland", "--device",
             "--no-inspect"}
# Shown as one three-way choice instead of two check boxes.
TMUX = ("--tmux", "--no-tmux")


def label(option):
    """The human label of OPTION (a long option name) in the current language."""
    pair = OPTION_LABELS.get(option)
    if not pair:
        return option
    return pair[1] if i18n.language() == "de" else pair[0]
