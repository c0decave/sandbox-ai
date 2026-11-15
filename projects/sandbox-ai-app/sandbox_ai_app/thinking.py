"""Whether an agent shows its model's thinking in the terminal: the launcher's --show-thinking on|off, as the Board's
agent part, its guide, the Start page and the new-project wizard offer it. No Qt.

The launcher maps it to each agent's own switch (agent_sandbox.AGENT_THINKING; a test keeps both the same): Claude
Code's showThinkingSummaries, Codex's hide_agent_reasoning and show_raw_agent_reasoning, OpenCode's thinking_mode,
Hermes' display.show_reasoning, sandbox-ai's chat for DSH, Goose's GOOSE_CLI_SHOW_THINKING. An agent without such a
switch (THINKING_NONE there: Qwen Code) is offered nothing to choose - the choice is greyed out with the reason."""
from __future__ import annotations

from .i18n import N_, tr

OPTION = "--show-thinking"
# The agent's own default ("on": shown, "off": hidden) for each agent that has a switch.
AGENT_DEFAULTS = {"claude": "off", "codex": "on", "opencode": "off", "hermes": "on", "dsh": "on", "goose": "off"}
# The switch each agent's --show-thinking sets, in words (the choice's tooltip).
SWITCHES = {"claude": N_("Claude Code's setting showThinkingSummaries"),
            "codex": N_("Codex's hide_agent_reasoning and show_raw_agent_reasoning"),
            "opencode": N_("OpenCode's thinking_mode (as /thinking in OpenCode switches it)"),
            "hermes": N_("Hermes' display.show_reasoning (as /reasoning show|hide)"),
            "dsh": N_("sandbox-ai's chat for DeepSeek Harness (/thinking on|off in the chat)"),
            "goose": N_("Goose's GOOSE_CLI_SHOW_THINKING")}
# Why an agent offers nothing to choose.
NO_SWITCH = {"qwen": N_("Qwen Code has no setting for it: its thinking is folded in the terminal, Ctrl+O (or Alt+T) "
                        "unfolds it in the session")}
VALUES = ("on", "off")


def available(agent):
    """Whether AGENT has a switch for it."""
    return agent in AGENT_DEFAULTS


def reason(agent):
    """Why AGENT offers no choice (translated), or None."""
    if available(agent):
        return None
    return tr(NO_SWITCH[agent]) if agent in NO_SWITCH else tr("This agent has no setting for it")


def choices(agent):
    """[(value, label)] for AGENT: its default first (""), then shown and hidden."""
    default = AGENT_DEFAULTS.get(agent)
    first = tr("the agent's default (shown)") if default == "on" else \
        tr("the agent's default (hidden)") if default == "off" else tr("the agent's default")
    return [("", first), ("on", tr("shown")), ("off", tr("hidden"))]


def tooltip(agent):
    """What the choice does for AGENT (translated)."""
    if not available(agent):
        return reason(agent)
    return tr("Shows or hides the model's thinking (reasoning) in the agent's terminal: --show-thinking, {switch}",
              switch=tr(SWITCHES[agent]))


def keep(props, agent):
    """The agent part's PROPS for AGENT: a choice it cannot take is dropped (in place)."""
    if props.get("thinking") and (not available(agent) or props["thinking"] not in VALUES):
        props.pop("thinking")
    return props
