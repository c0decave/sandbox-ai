#!/usr/bin/env python3
"""sandbox-ai notify: questions and alarms to your phone (ntfy, Telegram, WhatsApp, a webhook) while you are away.

  sandbox-ai notify list [--json]        the channels, how each last message went
  sandbox-ai notify add NAME KIND        a channel (NAME again: replaced); KIND is one of:
    --ntfy [URL/]TOPIC [--token-secret S]
    --telegram --bot-token-secret S --chat ID
    --whatsapp --phone-id ID --token-secret S --recipient NUMBER [--template T [--language L]]
    --webhook URL [--token-secret S]   or   --webhook-secret S (the URL itself is a secret)
  sandbox-ai notify add ... [--events LIST] [--when away|always] [--detail minimal|normal|full]
                            [--quiet HH:MM-HH:MM] [--rate SECONDS] [--api URL]
  sandbox-ai notify remove NAME
  sandbox-ai notify test NAME            send a test message now and say how it went
  sandbox-ai notify link [URL | --none]  the web interface's public URL, put into the messages
  sandbox-ai notify events               what each event is

Events: questions (a sandbox asks), alarms (a canary went off, a run lost its supervisor), safeguards (one refused
or ended something), limits (memory, CPU or processes at a run's limit), run-ended, run-failed, budget (a run used
80 % or all of a budget: sandbox-ai spend), update (a newer signed release, or one the update mode installed:
sandbox-ai update mode). Default: questions and alarms. --events all takes every one. When: away (the default: only while neither the desktop app nor the web
interface watches the questions - the times a desktop notification would show) or always. Alarms go out always,
also in quiet hours. Quiet hours (local time, e.g. 22:00-07:00) hold back everything else; it is not sent later.
--rate SECONDS (default 60, 0: no limit): the first event goes out at once, those that follow within that time go
as one message when it is over ("3 questions, 1 safeguard event").

What a message says (--detail): minimal - only "a question is waiting on HOST" (for services you do not trust with
more); normal (the default) - the kind, the run's name, a short what ("a network connection") and the time; full -
also the destination or path ("wants github.com:443"). Never a secret, a file's contents or a tool call's
arguments. Text a sandbox chose is shortened, control characters and markup characters are made harmless, and it is
sent as plain text (Telegram without parse_mode). Answering from the phone is not offered: with a link set
(notify link), the message carries the web interface's address, whose own login and TOTP protect it.

Tokens are only secret names (sandbox-ai secrets set NAME); ~/.config/sandbox-ai/notify.json (mode 0600) never
holds a token. Messages are sent from the host by the launcher or a session's supervisor, never from inside a
sandbox: nothing in it can trigger, change or read them. A failure is logged (launcher.log) and never holds up a
run; each request waits at most 10 seconds and follows no redirect. URLs are https; http only to this machine
(localhost, 127.0.0.1, ::1).

WhatsApp (the WhatsApp Business Cloud API of Meta) needs a Meta business app with a WhatsApp phone number: its
phone-number ID, a permanent access token (a system user's) as a secret, the recipient's number. WhatsApp lets a
business start a conversation only with an approved message template: create one (category utility) whose body
has one variable, {{1}}, wait for its approval and give --template NAME (--language its code, default en). Without
--template a plain text is sent, which WhatsApp delivers only within 24 hours after the recipient last wrote to
that number. A template's variable takes no line breaks: the message is sent as one line.

Standard library only.
"""
import argparse
import contextlib
import fcntl
import http.client
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = Path(__file__).resolve().parent
LOADING = threading.RLock()

KINDS = ("ntfy", "telegram", "whatsapp", "webhook")
# Event: (what it is, the minimal message, its plural for a summary)
EVENTS = {
    "questions": ("a sandbox asks (a connection, a lookup, a push, a tool call, a device, a URL to open)",
                  "a question is waiting on {host}", "questions"),
    "alarms": ("a canary went off, or a run lost its supervisor (sent always, also in quiet hours)",
               "an alarm on {host}", "alarms"),
    "safeguards": ("a safeguard refused or ended something in a run", "a safeguard acted on {host}", "safeguard events"),
    "limits": ("a run is at its memory, CPU or process limit", "a run is at a limit on {host}", "limit warnings"),
    "run-ended": ("a run ended (also stopped by you)", "a run ended on {host}", "runs ended"),
    "run-failed": ("a run ended with an error, or its start failed", "a run failed on {host}", "runs failed"),
    "budget": ("a run's model requests used 80 % or all of a budget (sandbox-ai spend)",
               "a budget is nearly or all used on {host}", "budget warnings"),
    "update": ("a newer signed release of sandbox-ai is there, or the update mode installed one (sandbox-ai update "
               "mode)", "a sandbox-ai update on {host}", "update notices"),
}
# One event of a kind in a summary ("3 questions, 1 safeguard event"): EVENTS' plural in the singular
SINGULAR = {"questions": "question", "alarms": "alarm", "safeguards": "safeguard event", "limits": "limit warning",
            "run-ended": "run ended", "run-failed": "run failed", "budget": "budget warning",
            "update": "update notice"}
DEFAULT_EVENTS = ["questions", "alarms"]
WHEN = ("away", "always")
DETAILS = ("minimal", "normal", "full")
DEFAULT_RATE = 60
TIMEOUT = 10  # seconds one request may take
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,31}")
SECRET = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
TOPIC = re.compile(r"[A-Za-z0-9_-]{1,64}")
CHAT = re.compile(r"-?[0-9]{1,20}|@[A-Za-z][A-Za-z0-9_]{4,31}")
PHONE_ID = re.compile(r"[0-9]{5,20}")
NUMBER = re.compile(r"\+?[0-9]{6,15}")
TEMPLATE = re.compile(r"[a-z0-9_]{1,512}")
LANGUAGE = re.compile(r"[a-z]{2,3}(?:_[A-Z]{2})?")
QUIET = re.compile(r"([01][0-9]|2[0-3]):([0-5][0-9])-([01][0-9]|2[0-3]):([0-5][0-9])")
BOT_TOKEN = re.compile(r"[0-9]{1,20}:[A-Za-z0-9_-]{20,100}")
DEFAULT_NTFY = "https://ntfy.sh"
TELEGRAM_API = "https://api.telegram.org"
WHATSAPP_API = "https://graph.facebook.com/v23.0"
PRIORITY = {"alarms": 5, "questions": 4, "run-failed": 4, "budget": 4}  # ntfy's 1..5 (3: default)
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def launch_log():
    """launchlog.py, the one copy of this process (launcher.log)."""
    loaded = sys.modules.get("sandbox_ai_launchlog")
    if loaded is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_launchlog", HERE / "launchlog.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        loaded = sys.modules.setdefault("sandbox_ai_launchlog", module)
    return loaded


LOG = launch_log()
_secret_store = None


def secret_store():
    global _secret_store
    if _secret_store is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_secret_store_for_notify", HERE / "secret_store.py")
        with LOADING:
            if _secret_store is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _secret_store = loaded
    return _secret_store


def get_secret(name):
    """The value of the secret NAME, never asked for on a terminal (a locked store is a ValueError: this runs beside
    an agent that has the terminal)."""
    return secret_store().get(name, ask=False)


# -- the file ---------------------------------------------------------------------------------------------------------

def path():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "sandbox-ai" / "notify.json"


def state_dir():
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "sandbox-ai" / "notify"


_filelock = None


def settings_lock(file):
    """The lock of a read-modify-write of FILE (filelock.py)."""
    global _filelock
    if _filelock is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_filelock", HERE / "filelock.py")
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        _filelock = loaded
    return _filelock.locked(file)


def load(file=None):
    """{"version": 1, "channels": [...], "link": URL or None} ({} of those without a file); ValueError for a damaged
    or unsafe file (not yours, not 0600, a link, a channel that does not check out)."""
    file = Path(file) if file else path()
    try:
        info = os.lstat(file)
    except FileNotFoundError:
        return {"version": 1, "channels": [], "link": None}
    if not os.path.isfile(file) or os.path.islink(file) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError(f"{file} must be a regular file of yours with mode 0600")
    try:
        data = json.loads(file.read_text())
    except RecursionError:
        raise ValueError(f"{file}: not valid JSON (nested too deeply)") from None
    except (OSError, ValueError) as exc:
        raise ValueError(f"{file}: {exc}") from None
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("channels"), list):
        raise ValueError(f"{file}: version 1 with a list of channels")
    channels = []
    for entry in data["channels"]:
        try:
            channels.append(check_channel(entry))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError(f"{file}: {exc}") from None
    names = [c["name"] for c in channels]
    if len(set(names)) != len(names):
        raise ValueError(f"{file}: a channel's name is there twice")
    link = data.get("link")
    if link is not None:
        link = check_link(link)
    return {"version": 1, "channels": channels, "link": link}


def save(document, file=None):
    file = Path(file) if file else path()
    file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = file.with_name(f".{file.name}.{os.getpid()}-{os.urandom(4).hex()}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump({"version": 1, "channels": document.get("channels", []), "link": document.get("link")},
                      stream, indent=2)
        os.replace(temporary, file)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


# -- checks -----------------------------------------------------------------------------------------------------------

def check_url(url, what="the URL", path=True):
    """URL (https, or http to this machine alone) without a user part, query or fragment: its text, trailing slash
    removed. PATH: a path may follow the host."""
    if not isinstance(url, str) or len(url) > 2000 or any(not (" " < c <= "~") for c in url):
        raise ValueError(f"{what} is an https URL of printable ASCII")
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    try:
        parts.port
    except ValueError:
        raise ValueError(f"{what}: the port is not a number from 1 to 65535") from None
    if parts.scheme not in ("https", "http") or not host or parts.username or parts.password or parts.query \
            or parts.fragment or (not path and parts.path not in ("", "/")):
        raise ValueError(f"{what} is https://HOST{'[/PATH]' if path else ''} (no user, no ?query, no #fragment): "
                         f"{url!r}")
    if parts.scheme == "http" and host not in LOCAL_HOSTS:
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            raise ValueError(f"{what}: http only to this machine (localhost); elsewhere https: {url!r}")
    return url.rstrip("/")


def check_link(link):
    if not isinstance(link, str):
        raise ValueError("the link is the web interface's https URL")
    return check_url(link, "the link")


def check_events(events):
    """The events of a list or of "a,b" / "all"; ValueError for an unknown one or none."""
    if isinstance(events, str):
        events = [part.strip() for part in events.split(",") if part.strip()]
        if events == ["all"]:
            events = list(EVENTS)
    if not isinstance(events, list) or not events or not all(isinstance(e, str) and e in EVENTS for e in events):
        raise ValueError(f"the events are one or more of {', '.join(EVENTS)} (or all)")
    return [e for e in EVENTS if e in events]


def check_quiet(value):
    if value is None:
        return None
    if not isinstance(value, str) or not QUIET.fullmatch(value) or value[:5] == value[6:]:
        raise ValueError("quiet hours are HH:MM-HH:MM (24 hours, local time; e.g. 22:00-07:00)")
    return value


def check_rate(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 86400:
        raise ValueError("the rate is a number of seconds from 0 to 86400")
    return value


def need(entry, key, pattern, what):
    value = entry.get(key)
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f"channel {entry.get('name')!r}: {what}")
    return value


def check_channel(entry):
    """ENTRY as a channel, its fields checked (unknown ones refused); ValueError otherwise."""
    if not isinstance(entry, dict):
        raise ValueError("each channel is an object")
    name = entry.get("name")
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ValueError("a channel's name is letters, digits, _ . - (up to 32, first a letter or digit)")
    kind = entry.get("kind")
    if kind not in KINDS:
        raise ValueError(f"channel {name!r}: the kind is one of {', '.join(KINDS)}")
    common = {"name", "kind", "events", "when", "detail", "quiet", "rate"}
    own = {"ntfy": {"url", "topic", "token_secret"}, "telegram": {"bot_token_secret", "chat", "api"},
           "whatsapp": {"phone_id", "token_secret", "to", "template", "language", "api"},
           "webhook": {"url", "url_secret", "token_secret"}}[kind]
    unknown = set(entry) - common - own
    if unknown:
        raise ValueError(f"channel {name!r}: unknown fields {', '.join(sorted(unknown))}")
    result = {"name": name, "kind": kind, "events": check_events(entry.get("events", DEFAULT_EVENTS)),
              "when": entry.get("when", "away"), "detail": entry.get("detail", "normal"),
              "quiet": check_quiet(entry.get("quiet")), "rate": check_rate(entry.get("rate", DEFAULT_RATE))}
    if result["when"] not in WHEN:
        raise ValueError(f"channel {name!r}: when is away or always")
    if result["detail"] not in DETAILS:
        raise ValueError(f"channel {name!r}: detail is {', '.join(DETAILS)}")
    for key in ("token_secret", "bot_token_secret", "url_secret"):
        if key in own and entry.get(key) is not None:
            result[key] = need(entry, key, SECRET, f"{key} is a secret's name (letters, digits, _)")
    if kind == "ntfy":
        result["url"] = check_url(entry.get("url", DEFAULT_NTFY), "the ntfy server")
        result["topic"] = need(entry, "topic", TOPIC, "the topic is letters, digits, _ and - (up to 64)")
    elif kind == "telegram":
        if "bot_token_secret" not in result:
            raise ValueError(f"channel {name!r}: --bot-token-secret names the bot's token (a secret)")
        result["chat"] = need(entry, "chat", CHAT, "the chat is a chat ID (a number) or @channelname")
        if entry.get("api") is not None:
            result["api"] = check_url(entry["api"], "--api")
    elif kind == "whatsapp":
        result["phone_id"] = need(entry, "phone_id", PHONE_ID, "the phone-number ID is digits")
        result["to"] = need(entry, "to", NUMBER, "the recipient is a phone number with its country code (+4917...)")
        if "token_secret" not in result:
            raise ValueError(f"channel {name!r}: --token-secret names the access token (a secret)")
        if entry.get("template") is not None:
            result["template"] = need(entry, "template", TEMPLATE, "a template's name is a-z, 0-9 and _")
            result["language"] = need({"name": name, "language": entry.get("language", "en")}, "language",
                                      LANGUAGE, "the language is a code such as en, de or en_US")
        elif entry.get("language") is not None:
            raise ValueError(f"channel {name!r}: --language goes with --template")
        if entry.get("api") is not None:
            result["api"] = check_url(entry["api"], "--api")
    else:
        if (entry.get("url") is None) == (entry.get("url_secret") is None):
            raise ValueError(f"channel {name!r}: a webhook has --webhook URL or --webhook-secret NAME (one of them)")
        if entry.get("url") is not None:
            result["url"] = check_url(entry["url"], "the webhook")
    return result


def where(channel):
    """Where CHANNEL sends to, as `list` shows it (no token)."""
    kind = channel["kind"]
    if kind == "ntfy":
        return f"{channel['url']}/{channel['topic']}"
    if kind == "telegram":
        return f"Telegram chat {channel['chat']}"
    if kind == "whatsapp":
        return f"WhatsApp {channel['to']}" + (f" (template {channel['template']})" if channel.get("template")
                                              else " (text: only within 24 h of their last message)")
    return channel["url"] if channel.get("url") else f"the URL in the secret {channel['url_secret']}"


def secret_names(channel):
    return [channel[key] for key in ("token_secret", "bot_token_secret", "url_secret") if channel.get(key)]


# -- what a message says ----------------------------------------------------------------------------------------------

HOST_NAME = None


def host_name():
    """This machine's name, as the messages name it (printable, short)."""
    global HOST_NAME
    if HOST_NAME is None:
        HOST_NAME = clean(socket.gethostname().split(".")[0] or "this host", 40)
    return HOST_NAME


def clean(value, limit=120, markup="<>`"):
    """VALUE (text a sandbox may have chosen) as harmless plain text of one line: control, format and other
    unprintable characters (bidi overrides, zero-width ones) become "?", runs of white space one space, and the
    characters of MARKUP a receiver might render (<...> links of a webhook's chat, `code`) plain ones; at most LIMIT
    characters ("..." where it was cut)."""
    text = "".join(c if c.isprintable() else ("?" if c not in "\n\t\r" else " ") for c in str(value))
    text = " ".join(text.split())
    plain = {"<": "(", ">": ")", "`": "'", "*": "-", "_": "-", "~": "-", "[": "(", "]": ")"}
    text = text.translate(str.maketrans({c: plain[c] for c in markup if c in plain}))
    return text if len(text) <= limit else text[:max(0, limit - 3)].rstrip() + "..."


WHATSAPP_MARKUP = "<>`*_~"  # WhatsApp renders *bold*, _italic_, ~strike~ and ```code``` in what it delivers


def render(channel, events, link=None, host=None):
    """(title, text, priority) of the message for EVENTS (one or more {"event", "run", "what", "detail", "time"}) on
    CHANNEL, as its detail allows."""
    host = host or host_name()
    detail = channel.get("detail", "normal")
    kinds = [e["event"] for e in events]
    priority = max(PRIORITY.get(k, 3) for k in kinds)
    if len(events) == 1:
        event = events[0]
        minimal = EVENTS[event["event"]][1].format(host=host)
        if detail == "minimal":
            return "sandbox-ai", minimal, priority
        what = clean(event.get("what") or minimal, 120)
        if detail == "full" and event.get("detail"):
            what += f": {clean(event['detail'], 160)}"
        run = clean(event["run"], 60) if event.get("run") else ""
        at = time.strftime("%H:%M", time.localtime(event.get("time") or time.time()))
        lines = [f"{run}: {what}" if run else what, f"on {host} at {at}"]
        if link:
            lines.append(link)
        return f"sandbox-ai: {event['event'].replace('-', ' ')}", "\n".join(lines), priority
    counts = {}
    for kind in kinds:
        counts[kind] = counts.get(kind, 0) + 1
    summary = ", ".join(f"{n} {EVENTS[k][2] if n > 1 else SINGULAR[k]}" for k, n in counts.items())
    if detail == "minimal":
        return "sandbox-ai", f"{summary} on {host}", priority
    runs = []
    for event in events:
        if event.get("run") and clean(event["run"], 60) not in runs:
            runs.append(clean(event["run"], 60))
    lines = [f"{summary} on {host}"]
    if runs:
        lines.append("runs: " + ", ".join(runs[:5]) + (f" and {len(runs) - 5} more" if len(runs) > 5 else ""))
    first, last = min(e.get("time") or 0 for e in events), max(e.get("time") or 0 for e in events)
    lines.append(f"{time.strftime('%H:%M', time.localtime(first))}-{time.strftime('%H:%M', time.localtime(last))}")
    if link:
        lines.append(link)
    return f"sandbox-ai: {len(events)} events", "\n".join(lines), priority


# -- sending ----------------------------------------------------------------------------------------------------------

class NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is an error: the token in a header must never follow it to another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, f"redirected (not followed): {msg}", headers, fp)


OPENER = urllib.request.build_opener(NoRedirect)


class SendError(Exception):
    pass


def request_of(channel, title, text, priority, secret=get_secret, link=None):
    """(url, headers, body bytes, [secret values]) of the request that sends TITLE and TEXT on CHANNEL."""
    kind, values = channel["kind"], []

    def value(name):
        try:
            got = secret(name)
        except ValueError as exc:  # locked: sandbox-ai secrets unlock
            raise SendError(f"the secret {name} cannot be read: {exc}") from None
        if not got:
            raise SendError(f"the secret {name} is not kept: sandbox-ai secrets set {name}")
        if any(ord(c) < 33 or ord(c) == 127 for c in got):
            raise SendError(f"the secret {name} is not usable (it holds spaces or control characters)")
        values.append(got)
        return got
    headers = {"Content-Type": "application/json", "User-Agent": "sandbox-ai"}
    if kind == "ntfy":
        body = {"topic": channel["topic"], "title": title, "message": text, "priority": priority,
                "tags": ["rotating_light"] if priority == 5 else []}
        if link:
            body["click"] = link
        if channel.get("token_secret"):
            headers["Authorization"] = f"Bearer {value(channel['token_secret'])}"
        return channel["url"] + "/", headers, json.dumps(body).encode(), values
    if kind == "telegram":
        token = value(channel["bot_token_secret"])
        if not BOT_TOKEN.fullmatch(token):
            raise SendError(f"the secret {channel['bot_token_secret']} is not a Telegram bot token (123456:ABC...)")
        chat = int(channel["chat"]) if channel["chat"].lstrip("-").isdigit() else channel["chat"]
        # Plain text: no parse_mode, so nothing in it is read as Markdown or HTML.
        body = {"chat_id": chat, "text": f"{title}\n{text}", "disable_web_page_preview": True}
        return f"{channel.get('api') or TELEGRAM_API}/bot{token}/sendMessage", headers, json.dumps(body).encode(), \
            values
    if kind == "whatsapp":
        headers["Authorization"] = f"Bearer {value(channel['token_secret'])}"
        to = channel["to"].lstrip("+")
        if channel.get("template"):
            # A template's variable takes no line break, no tab and no more than four spaces in a row.
            line = " - ".join(part for part in (title, *text.split("\n")) if part)
            body = {"messaging_product": "whatsapp", "to": to, "type": "template",
                    "template": {"name": channel["template"], "language": {"code": channel.get("language", "en")},
                                 "components": [{"type": "body", "parameters": [
                                     {"type": "text", "text": clean(line, 900, WHATSAPP_MARKUP)}]}]}}
        else:
            body = {"messaging_product": "whatsapp", "to": to, "type": "text",
                    "text": {"body": "\n".join(line if line == link else clean(line, 900, WHATSAPP_MARKUP)
                                                for line in (title, *text.split("\n"))), "preview_url": False}}
        return f"{channel.get('api') or WHATSAPP_API}/{channel['phone_id']}/messages", headers, \
            json.dumps(body).encode(), values
    url = channel.get("url")
    if url is None:
        url = value(channel["url_secret"])
        try:
            url = check_url(url, f"the URL in the secret {channel['url_secret']}")
        except ValueError as exc:
            raise SendError(str(exc).replace(url, "(the secret's value)")) from None
    if channel.get("token_secret"):
        headers["Authorization"] = f"Bearer {value(channel['token_secret'])}"
    return url, headers, None, values


def webhook_body(title, text, events, host):
    return {"source": "sandbox-ai", "host": host, "title": title, "text": f"{title}\n{text}",
            "events": [{"event": e["event"], "run": clean(e["run"], 60) if e.get("run") else None,
                        "what": clean(e.get("what") or "", 120),
                        "time": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(e.get("time") or time.time()))}
                       for e in events]}


def redact(text, values):
    text = str(text)
    for value in values:
        if value:
            text = text.replace(value, "***")
            text = text.replace(urllib.parse.quote(value, safe=""), "***")
    return text


def send(channel, events, link=None, secret=get_secret, opener=None, timeout=TIMEOUT, host=None):
    """Send EVENTS as one message on CHANNEL; None when it went out, else why not (no secret in it)."""
    values = []
    try:
        title, text, priority = render(channel, events, link, host)
        detail = channel.get("detail", "normal")
        url, headers, body, values = request_of(channel, title, text, priority, secret,
                                                link if detail != "minimal" else None)
        if body is None:  # the webhook: the events as JSON (as the detail allows)
            body = json.dumps(webhook_body(title, text, events if detail != "minimal" else [], host or host_name())
                              ).encode()
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with (opener or OPENER).open(request, timeout=timeout) as response:
            response.read(65536)
        return None
    except SendError as exc:
        return redact(exc, values)
    except urllib.error.HTTPError as exc:
        said = ""
        with contextlib.suppress(Exception):
            said = exc.read(2000).decode("utf-8", "replace")
        return redact(f"the server answered {exc.code} {exc.reason}" + (f": {clean(said, 300)}" if said else ""),
                      values)
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as exc:
        reason = getattr(exc, "reason", None) or exc
        return redact(f"not sent: {clean(reason, 300)}", values)


# -- when, and how often ----------------------------------------------------------------------------------------------

def in_quiet(quiet, now=None):
    """NOW (epoch, local time) lies in the quiet hours QUIET ("HH:MM-HH:MM", over midnight too)."""
    if not quiet:
        return False
    match = QUIET.fullmatch(quiet)
    if not match:
        return False
    start = int(match[1]) * 60 + int(match[2])
    end = int(match[3]) * 60 + int(match[4])
    local = time.localtime(now if now is not None else time.time())
    minute = local.tm_hour * 60 + local.tm_min
    return start <= minute < end if start < end else minute >= start or minute < end


@contextlib.contextmanager
def channel_state(name):
    """The state of channel NAME ({"last", "pending", "status"}), locked against the other launchers; written back."""
    directory = state_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(directory / f"{name}.lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | os.O_CLOEXEC,
                 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        file = directory / f"{name}.json"
        try:
            state = json.loads(file.read_text())
        except (OSError, ValueError, RecursionError):
            state = {}
        if not isinstance(state, dict):
            state = {}
        state.setdefault("last", 0)
        state["pending"] = state.get("pending") if isinstance(state.get("pending"), list) else []
        before = json.dumps(state, sort_keys=True)
        yield state
        if json.dumps(state, sort_keys=True) != before:
            temporary = directory / f".{name}.{os.getpid()}-{threading.get_ident()}.json"
            temporary.write_text(json.dumps(state))
            os.chmod(temporary, 0o600)
            os.replace(temporary, file)
    finally:
        os.close(fd)


PENDING_LIMIT = 200  # events held for one summary


def record_result(name, error):
    with contextlib.suppress(OSError):
        with channel_state(name) as state:
            state["status"] = {"time": int(time.time()), "ok": error is None, **({"error": error[:300]} if error
                                                                                  else {})}


def deliver(channel, events, link, sender):
    """Send EVENTS on CHANNEL (SENDER: send), logged, its result kept for `list`; never an error."""
    error = None
    try:
        error = sender(channel, events, link)
    except Exception as exc:  # noqa: BLE001 - a notification never ends what sent it
        error = f"failed: {clean(exc, 300)}"
    kinds = ",".join(sorted({e["event"] for e in events}))
    if error is None:
        LOG.info("notify", f"{channel['name']} ({channel['kind']}): sent {len(events)} event(s) ({kinds})")
    else:
        LOG.warning("notify", f"{channel['name']} ({channel['kind']}): {kinds} not sent: {error}")
    record_result(channel["name"], error)
    return error


class Dispatcher:
    """Hands events to the channels: who gets which, when, how often. SENDER(channel, events, link) -> error or None
    (send); WATCHED() -> the app or the web interface watches; CLOCK: epoch seconds."""

    def __init__(self, sender=None, watched=None, clock=time.time, threaded=True):
        self.sender = sender or (lambda channel, events, link: send(channel, events, link))
        self.watched = watched or (lambda: False)
        self.clock, self.threaded = clock, threaded
        self.threads, self.timers, self.lock = [], {}, threading.Lock()

    def start(self, function, *args):
        if not self.threaded:
            function(*args)
            return None
        thread = threading.Thread(target=function, args=args, daemon=True, name="sandbox-ai-notify")
        thread.start()
        with self.lock:
            self.threads = [t for t in self.threads if t.is_alive()] + [thread]
        return thread

    def tell(self, event, run=None, what=None, detail=None, config=None):
        """EVENT to every channel that takes it now. Returns the names of the channels it went to (sent, or held for
        a summary)."""
        if event not in EVENTS:
            raise ValueError(f"no event {event!r}")
        document = config if config is not None else load()
        now = self.clock()
        item = {"event": event, "run": run, "what": what, "detail": detail, "time": now}
        watched, taken = None, []
        for channel in document.get("channels", []):
            if event not in channel["events"]:
                continue
            alarm = event == "alarms"
            if not alarm and in_quiet(channel.get("quiet"), now):
                LOG.debug("notify", f"{channel['name']}: {event} not sent (quiet hours {channel['quiet']})")
                continue
            if not alarm and channel.get("when", "away") == "away":
                if watched is None:
                    try:
                        watched = bool(self.watched())
                    except Exception:  # noqa: BLE001 - unknown: as if nobody watched
                        watched = False
                if watched:
                    LOG.debug("notify", f"{channel['name']}: {event} not sent (the app or the web interface watches)")
                    continue
            taken.append(channel["name"])
            self.offer(channel, item, document.get("link"))
        return taken

    def offer(self, channel, item, link):
        """ITEM now, or held for the summary when the channel sent within its rate; alarms go at once."""
        rate = channel.get("rate", DEFAULT_RATE)
        now = item["time"]
        try:
            with channel_state(channel["name"]) as state:
                due = now - state["last"] >= rate or rate == 0 or item["event"] == "alarms"
                if not due:
                    if len(state["pending"]) < PENDING_LIMIT:
                        state["pending"].append(item)
                    wait = max(0.0, state["last"] + rate - now)
                    later = True
                else:
                    # Held ones of a process that ended before its summary go along with this one.
                    batch = [e for e in state["pending"] if isinstance(e, dict) and e.get("event") in EVENTS] + [item]
                    state["pending"], state["last"], later = [], now, False
        except OSError as exc:
            LOG.warning("notify", f"{channel['name']}: its state could not be kept ({exc}); sent without a limit")
            batch, later = [item], False
        if later:
            self.schedule(channel, link, wait)
        else:
            self.start(deliver, channel, batch, link, self.sender)

    def schedule(self, channel, link, wait):
        with self.lock:
            timer = self.timers.get(channel["name"])
            if timer is not None and timer.is_alive():
                return
            if not self.threaded:
                self.timers[channel["name"]] = None
                return
            timer = threading.Timer(wait + 0.05, self.flush, args=(channel, link))
            timer.daemon = True
            self.timers[channel["name"]] = timer
            timer.start()

    def flush(self, channel, link, final=False):
        """The summary of what CHANNEL held, once its time is over (FINAL: now)."""
        with self.lock:
            self.timers.pop(channel["name"], None)
        rate = channel.get("rate", DEFAULT_RATE)
        try:
            with channel_state(channel["name"]) as state:
                now = self.clock()
                if not state["pending"]:
                    return None
                if in_quiet(channel.get("quiet"), now):  # (held before they began: not sent in them, nor later)
                    LOG.debug("notify", f"{channel['name']}: {len(state['pending'])} held event(s) not sent (quiet "
                                        f"hours {channel['quiet']})")
                    state["pending"] = []
                    return None
                if not final and now - state["last"] < rate:
                    wait = state["last"] + rate - now
                    batch = None
                else:
                    batch = [e for e in state["pending"] if isinstance(e, dict) and e.get("event") in EVENTS]
                    state["pending"], state["last"] = [], now
        except OSError:
            return None
        if batch is None:
            self.schedule(channel, link, wait)
            return None
        return self.start(deliver, channel, batch, link, self.sender) if batch else None

    def finish(self, seconds=5.0, config=None):
        """At the end of the process: what is held is sent now, and the messages under way get up to SECONDS."""
        with self.lock:
            timers = [t for t in self.timers.values() if t is not None]
            self.timers = {}
        for timer in timers:
            timer.cancel()
        with contextlib.suppress(Exception):
            document = config if config is not None else load()
            for channel in document.get("channels", []):
                self.flush(channel, document.get("link"), final=True)
        deadline = time.monotonic() + seconds
        with self.lock:
            threads = list(self.threads)
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))


DISPATCHER = None


def dispatcher(watched=None):
    global DISPATCHER
    with LOADING:
        if DISPATCHER is None:
            DISPATCHER = Dispatcher(watched=watched)
        elif watched is not None:
            DISPATCHER.watched = watched
    return DISPATCHER


def tell(event, run=None, what=None, detail=None, watched=None):
    """EVENT to the phone channels (sandbox-ai notify), from the host's launcher or supervisor: never an error, never
    waits for the network. WHAT: in general words (no host, no path); DETAIL: the destination or path (only
    channels with --detail full get it); RUN: the run's name; WATCHED() says whether the app or the web watches."""
    try:
        document = load()
    except ValueError as exc:
        LOG.warning("notify", f"no phone notification: {exc}")
        return []
    if not document["channels"]:
        return []
    try:
        return dispatcher(watched).tell(event, run, what, detail, document)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("notify", f"no phone notification of {event}", exc)
        return []


def finish(seconds=5.0):
    """Before the launcher ends: what is held is sent, what is under way gets up to SECONDS."""
    if DISPATCHER is not None:
        with contextlib.suppress(Exception):
            DISPATCHER.finish(seconds)


# -- the command ------------------------------------------------------------------------------------------------------

def status_of(name):
    try:
        state = json.loads((state_dir() / f"{name}.json").read_text())
    except (OSError, ValueError, RecursionError):
        return None
    status = state.get("status") if isinstance(state, dict) else None
    return status if isinstance(status, dict) else None


def listing(document):
    return {"version": 1, "link": document.get("link"), "events": {k: v[0] for k, v in EVENTS.items()},
            "kinds": list(KINDS), "default_events": DEFAULT_EVENTS,
            "channels": [{**channel, "where": where(channel), "secrets": secret_names(channel),
                          "status": status_of(channel["name"])} for channel in document["channels"]]}


# The fields of a channel as notify.json names them: what the web interface and the app send for one.
FIELDS = ("name", "kind", "url", "topic", "token_secret", "bot_token_secret", "chat", "api", "phone_id", "to",
          "template", "language", "url_secret", "events", "when", "detail", "quiet", "rate")


def add_arguments(fields):
    """The `sandbox-ai notify add` arguments that make the channel of FIELDS (notify.json's names), checked as the
    command checks them (ValueError otherwise): the web interface and the app build theirs here."""
    channel = check_channel({key: value for key, value in fields.items() if value not in (None, "")})
    kind = channel["kind"]
    args = ["notify", "add"]
    if kind == "ntfy":
        args.append(f"--ntfy={channel['url']}/{channel['topic']}")
    elif kind == "telegram":
        args += ["--telegram", f"--bot-token-secret={channel['bot_token_secret']}", f"--chat={channel['chat']}"]
    elif kind == "whatsapp":
        args += ["--whatsapp", f"--phone-id={channel['phone_id']}", f"--recipient={channel['to']}"]
        if channel.get("template"):
            args += [f"--template={channel['template']}", f"--language={channel['language']}"]
    elif channel.get("url"):
        args.append(f"--webhook={channel['url']}")
    else:
        args.append(f"--webhook-secret={channel['url_secret']}")
    if channel.get("token_secret"):
        args.append(f"--token-secret={channel['token_secret']}")
    if channel.get("api"):
        args.append(f"--api={channel['api']}")
    args += [f"--events={','.join(channel['events'])}", f"--when={channel['when']}", f"--detail={channel['detail']}",
             f"--rate={channel['rate']}"]
    if channel.get("quiet"):
        args.append(f"--quiet={channel['quiet']}")
    return [*args, "--", channel["name"]]


def channel_from(args):
    """The channel of `notify add` ARGS."""
    kinds = [k for k in KINDS if getattr(args, k) not in (None, False)] + (["webhook"] if args.webhook_secret
                                                                             else [])
    if len(set(kinds)) != 1 or (args.webhook and args.webhook_secret):
        raise ValueError("give one of --ntfy, --telegram, --whatsapp, --webhook (or --webhook-secret)")
    kind = kinds[0]
    entry = {"name": args.name, "kind": kind}
    for key in ("when", "detail", "quiet", "rate"):
        if getattr(args, key) is not None:
            entry[key] = getattr(args, key)
    if args.events is not None:
        entry["events"] = check_events(args.events)
    misplaced = {"ntfy": ["bot_token_secret", "chat", "phone_id", "recipient", "template", "language", "api"],
                 "telegram": ["token_secret", "phone_id", "recipient", "template", "language"],
                 "whatsapp": ["bot_token_secret", "chat"],
                 "webhook": ["bot_token_secret", "chat", "phone_id", "recipient", "template", "language", "api"]}[kind]
    wrong = [key for key in misplaced if getattr(args, key) is not None]
    if wrong:
        raise ValueError(f"--{kind} takes no {', '.join('--' + key.replace('_', '-') for key in wrong)}")
    if kind == "ntfy":
        value = args.ntfy
        base, slash, topic = value.rpartition("/")
        entry["url"] = base if slash else DEFAULT_NTFY
        entry["topic"] = topic
        if not TOPIC.fullmatch(topic or ""):
            raise ValueError("--ntfy is [https://SERVER/]TOPIC; the topic is letters, digits, _ and - (up to 64)")
    for key in ("token_secret", "bot_token_secret", "chat", "phone_id", "template", "language", "api"):
        if getattr(args, key) is not None:
            entry[key] = getattr(args, key)
    if args.recipient is not None:
        entry["to"] = args.recipient
    if kind == "webhook":
        if args.webhook:
            entry["url"] = args.webhook
        else:
            entry["url_secret"] = args.webhook_secret
    return check_channel(entry)


def help_style(prog):
    loaded = sys.modules.get("sandbox_ai_helpstyle")
    if loaded is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_helpstyle", HERE / "helpstyle.py")
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        sys.modules["sandbox_ai_helpstyle"] = loaded
    return loaded.CommandsFormatter(prog)


def parser():
    p = argparse.ArgumentParser(prog="sandbox-ai notify", description=__doc__.split("\n\n")[0],
                                formatter_class=help_style, allow_abbrev=False,
                                epilog="\n\n".join(__doc__.split("\n\n")[1:3]))
    commands = p.add_subparsers(dest="command", required=True)
    listed = commands.add_parser("list", help="the channels (secret names only)")
    listed.add_argument("--json", action="store_true", help="print the result as JSON (for programs)")
    added = commands.add_parser("add", help="a channel (one with that name is replaced)")
    added.add_argument("name", help="the channel's name (letters, digits, _ . -)")
    kind = added.add_argument_group("kind (one of them)")
    kind.add_argument("--ntfy", metavar="URL/TOPIC", help="ntfy: [https://SERVER/]TOPIC (default server ntfy.sh)")
    kind.add_argument("--telegram", action="store_true", help="Telegram: a bot's sendMessage to --chat")
    kind.add_argument("--whatsapp", action="store_true", help="WhatsApp Business Cloud API to --recipient")
    kind.add_argument("--webhook", metavar="URL", help="a webhook: HTTPS POST of JSON")
    kind.add_argument("--webhook-secret", metavar="NAME", help="a webhook whose URL is the secret NAME (a URL "
                                                              "that carries its key)")
    own = added.add_argument_group("the kind's settings")
    own.add_argument("--token-secret", metavar="NAME", help="the secret holding the access token (ntfy, WhatsApp, "
                                                            "webhook: sent as a bearer token)")
    own.add_argument("--bot-token-secret", metavar="NAME", help="Telegram: the secret holding the bot's token")
    own.add_argument("--chat", metavar="ID", help="Telegram: the chat ID (a number) or @channelname")
    own.add_argument("--phone-id", metavar="ID", help="WhatsApp: the sending phone number's ID")
    own.add_argument("--recipient", metavar="NUMBER", help="WhatsApp: the recipient's number with its country code")
    own.add_argument("--template", metavar="NAME", help="WhatsApp: an approved template with one body variable")
    own.add_argument("--language", metavar="CODE", help="WhatsApp: the template's language (default en)")
    own.add_argument("--api", metavar="URL", help="Telegram, WhatsApp: another API base URL (a local Bot API "
                                                  "server, another Graph API version)")
    common = added.add_argument_group("what and when")
    common.add_argument("--events", metavar="LIST", help=f"Comma-separated: {', '.join(EVENTS)}, or all "
                                                         f"(default: {','.join(DEFAULT_EVENTS)})")
    common.add_argument("--when", choices=WHEN, help="away (default): while neither app nor web interface watches; "
                                                     "always")
    common.add_argument("--detail", choices=DETAILS, help="minimal, normal (default) or full (destinations, paths)")
    common.add_argument("--quiet", metavar="HH:MM-HH:MM", help="Quiet hours, local time (alarms still go)")
    common.add_argument("--rate", metavar="SECONDS", type=int, help=f"At most one message per SECONDS, the rest "
                                                                    f"summed up (default {DEFAULT_RATE}; 0: no limit)")
    removed = commands.add_parser("remove", help="forget a channel")
    removed.add_argument("name")
    tested = commands.add_parser("test", help="send a test message now")
    tested.add_argument("name")
    linked = commands.add_parser("link", help="the web interface's public URL in the messages")
    linked.add_argument("url", nargs="?", help="https://... of your web interface (sandbox-ai web)")
    linked.add_argument("--none", action="store_true", help="no link")
    commands.add_parser("events", help="what each event is")
    return p


def main(argv, out=print):
    args = parser().parse_args(argv)
    try:
        with settings_lock(path()) if args.command in ("add", "remove", "link") else contextlib.nullcontext():
            return run_command(args, out)
    except ValueError as exc:
        print(f"sandbox-ai notify: {exc}", file=sys.stderr)
        return 1


def run_command(args, out):
    try:
        if args.command == "events":
            for event, (text, _, _) in EVENTS.items():
                out(f"{event:<11} {text}{' (default)' if event in DEFAULT_EVENTS else ''}")
            return 0
        document = load()
        if args.command == "list":
            if args.json:
                out(json.dumps(listing(document), indent=2))
                return 0
            for channel in listing(document)["channels"]:
                status = channel["status"]
                said = "" if not status else (" - last sent " if status.get("ok") else " - last failed ") + \
                    time.strftime("%Y-%m-%d %H:%M", time.localtime(status.get("time") or 0)) + \
                    ("" if status.get("ok") else f": {status.get('error', '')}")
                out(f"{channel['name']:<14} {channel['kind']:<9} {channel['where']}")
                out(f"{'':<14} events {','.join(channel['events'])}; when {channel['when']}; detail "
                    f"{channel['detail']}; rate {channel['rate']} s" + (f"; quiet {channel['quiet']}"
                                                                         if channel["quiet"] else "")
                    + (f"; secrets {', '.join(channel['secrets'])}" if channel["secrets"] else "") + said)
            if not document["channels"]:
                out("no channels; add one, e.g.: sandbox-ai notify add phone --ntfy https://ntfy.sh/A-LONG-RANDOM-TOPIC")
            if document.get("link"):
                out(f"link in the messages: {document['link']}")
            return 0
        if args.command == "add":
            channel = channel_from(args)
            kept = [c for c in document["channels"] if c["name"] != channel["name"]]
            replaced = len(kept) != len(document["channels"])
            save({**document, "channels": kept + [channel]})
            out(f"{channel['name']}: {'replaced' if replaced else 'added'} - {where(channel)}; events "
                f"{','.join(channel['events'])}; when {channel['when']}; detail {channel['detail']}")
            store = secret_store()
            for name in secret_names(channel):
                with contextlib.suppress(ValueError):  # locked: it is there, just not readable now
                    if store.get(name, ask=False) is None:
                        out(f"note: the secret {name} is not kept yet: sandbox-ai secrets set {name}")
            if channel["kind"] == "ntfy" and channel["url"] == DEFAULT_NTFY and not channel.get("token_secret") \
                    and len(channel["topic"]) < 20:
                out("note: on ntfy.sh whoever knows the topic reads its messages: take a long random one (20 "
                    "characters or more), or a server of your own with a token")
            if channel["kind"] == "whatsapp" and not channel.get("template"):
                out("note: without --template WhatsApp delivers the text only within 24 hours after the recipient "
                    "last wrote to that number; an approved template works always")
            out(f"try it: sandbox-ai notify test {channel['name']}")
            return 0
        if args.command == "remove":
            kept = [c for c in document["channels"] if c["name"] != args.name]
            if len(kept) == len(document["channels"]):
                raise ValueError(f"no channel {args.name}")
            save({**document, "channels": kept})
            with contextlib.suppress(OSError):
                for suffix in (".json", ".lock"):
                    (state_dir() / f"{args.name}{suffix}").unlink(missing_ok=True)
            out(f"{args.name}: removed")
            return 0
        if args.command == "link":
            if args.none == (args.url is not None):
                raise ValueError("give the URL, or --none")
            link = None if args.none else check_link(args.url)
            save({**document, "link": link})
            out(f"link in the messages: {link}" if link else "no link in the messages")
            return 0
        channel = next((c for c in document["channels"] if c["name"] == args.name), None)
        if channel is None:
            raise ValueError(f"no channel {args.name}")
        item = {"event": "questions", "run": "test", "what": "a test message of sandbox-ai notify",
                "detail": "nothing waits", "time": time.time()}
        error = send(channel, [item], document.get("link"))
        record_result(channel["name"], error)
        if error:
            LOG.warning("notify", f"{channel['name']}: the test message was not sent: {error}")
            print(f"sandbox-ai notify: {channel['name']}: {error}", file=sys.stderr)
            return 1
        LOG.info("notify", f"{channel['name']}: the test message was sent")
        out(f"{channel['name']}: sent - {where(channel)}")
        return 0
    except ValueError as exc:
        print(f"sandbox-ai notify: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
