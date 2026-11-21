#!/usr/bin/env python3
"""sandbox-ai spend: what the model requests of your runs cost, and budgets that stop a run before it costs more.

  sandbox-ai spend                         the spending by run (this project's and every other's), newest first
  sandbox-ai spend --by project|agent|provider|model|day|billing [--since 7d|DATE] [--project DIR] [--run RUN]
  sandbox-ai spend run RUN                 one run: cost so far, its budgets and how much of each is used
  sandbox-ai spend raise RUN USD           a running run may spend USD more than its budgets (also when refused)
  sandbox-ai spend prices                  the price table: built in (sandbox/prices.json) with yours over it
  sandbox-ai spend prices set MODEL in=USD out=USD [cached=USD] [write=USD] [--provider P]
  sandbox-ai spend prices remove MODEL [--provider P]
  sandbox-ai spend limit                   your limits per day or month
  sandbox-ai spend limit set (--project [DIR] | --all) (--day | --month) USD
  sandbox-ai spend limit remove (--project [DIR] | --all) (--day | --month)

The connection proxy counts every model request's tokens from its answer (streamed or not; Anthropic Messages, OpenAI
Responses and Chat Completions and the APIs like them, the --llm servers): input, input read from and written to the
cache, output and its reasoning - by run, project, agent, provider, model and day. It prices them with the table
(USD per million tokens) and writes one small line per request to spend-TIME-RUN.jsonl in the run's log directory
(removed with the other logs after log_days, never before its month is over: a month's limit counts it). A local
model (an --llm server of your own) costs 0. A model the table does not know is counted in tokens with the cost
"unknown", and said so; a price set later prices it. On a run with a budget it is counted at the fallback price
instead (the table's highest input and output prices: no way around the budget), said with the model and the price;
and a streamed Chat Completions request is asked for its usage (stream_options.include_usage, added by the proxy). A login to a plan (Claude Pro/Max, a ChatGPT plan) has no price
per token: its tokens are counted, the cost shows "subscription".

Budgets: --budget USD for one start (or "budget" in your defaults or the project's .sandbox-ai.json), and limits per
day or month for a project or for everything (spend limit set). At 80 % you are told (a desktop notification and the
phone channels that take the event "budget": sandbox-ai notify); at 100 % the proxy refuses the run's next model
request with a sandbox-ai: error the agent shows - a request under way is never cut, and local models and
subscriptions go on. Raise it during the run: sandbox-ai spend raise RUN USD, or the button in the app or the web
interface. A day's or a month's limit counts what every run spent in it (other runs that run meanwhile within about
half a minute).

Standard library only.
"""
import argparse
import calendar
import contextlib
import datetime
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
import zlib

HERE = Path(__file__).resolve().parent
LOADING = threading.RLock()
_modules = {}

PRICES = HERE / "prices.json"
FILE = re.compile(r"spend-(\d{8}-\d{6})-([A-Za-z0-9._-]{1,128}?)\.jsonl(?:\.(\d{1,3}))?(?:\.gz)?")
BY = ("run", "project", "agent", "provider", "model", "day", "billing")
PERIODS = ("day", "month")
TOKENS = ("in", "cached", "write", "write_1h", "out", "reasoning")
PRICE_KEYS = ("in", "out", "cached", "write", "write_1h")
MODEL = re.compile(r"[\w.:/@+-]{1,200}", re.ASCII)
PROVIDER = re.compile(r"[a-z0-9][a-z0-9_.:-]{0,63}")
USD_LIMIT = 1_000_000
LINE_LIMIT = 1 << 16
READ_LIMIT = 256 << 20       # bytes of spend logs read for one answer at most (the newest kept)
WATCH_SECONDS = 5            # how often the launcher looks for the proxy's budget warnings
OTHERS_SECONDS = 30          # how often it tells the proxy what other runs spent of a shared limit


def module(name):
    """A sibling module of sandbox-ai, loaded once."""
    with LOADING:
        if name not in _modules:
            spec = importlib.util.spec_from_file_location(f"sandbox_{name}_for_spend", HERE / f"{name}.py")
            loaded = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(loaded)
            _modules[name] = loaded
        return _modules[name]


def runtime():
    return module("runtime")


def printable(text):
    return "".join(c if c.isprintable() else f"\\x{ord(c):02x}" if ord(c) < 256 else f"\\u{ord(c):04x}"
                   for c in str(text))


# -- settings: your prices and limits ----------------------------------------------------------------------------------

def config_path():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "sandbox-ai" / "spend.json"


def logs_root():
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "sandbox-ai" / "logs"


def check_usd(value, what="USD"):
    """VALUE as a number of USD (0 to USD_LIMIT); ValueError otherwise."""
    try:
        number = float(str(value).strip().removeprefix("$"))
    except ValueError:
        raise ValueError(f"{what}: {printable(value)!r} is no number of USD") from None
    if not 0 <= number <= USD_LIMIT or number != number:
        raise ValueError(f"{what}: from 0 to {USD_LIMIT} USD")
    return round(number, 6)


def check_price(entry, what):
    if not isinstance(entry, dict) or not set(entry) <= {*PRICE_KEYS, "long"} or "in" not in entry or \
            "out" not in entry:
        raise ValueError(f"{what}: a price has in and out (and cached, write, write_1h: USD per million tokens)")
    for key in PRICE_KEYS:
        if key in entry and (isinstance(entry[key], bool) or not isinstance(entry[key], (int, float))
                             or not 0 <= entry[key] <= 100000):
            raise ValueError(f"{what}: {key} is USD per million tokens (0 to 100000)")
    if "long" in entry:
        long = entry["long"]
        if not isinstance(long, dict) or not isinstance(long.get("above"), int) or \
                not set(long) <= {*PRICE_KEYS, "above"}:
            raise ValueError(f"{what}: long is {{\"above\": TOKENS, and prices}}")
    return entry


def load_settings(path=None):
    """{"version": 1, "prices": {provider: {model: price}}, "limits": [{"scope", "project", "path", "period",
    "usd"}]}; ValueError for a damaged file."""
    path = Path(path) if path else config_path()
    try:
        document = json.loads(path.read_text())
    except FileNotFoundError:
        return {"version": 1, "prices": {}, "limits": []}
    except (OSError, ValueError, RecursionError) as exc:
        raise ValueError(f"{path}: {exc}") from None
    if not isinstance(document, dict) or document.get("version") != 1 or \
            not set(document) <= {"version", "prices", "limits"}:
        raise ValueError(f"{path}: expected version 1 with prices and limits")
    prices = document.get("prices") or {}
    if not isinstance(prices, dict):
        raise ValueError(f"{path}: prices is {{provider: {{model: price}}}}")
    for provider, table in prices.items():
        if not PROVIDER.fullmatch(str(provider)) or not isinstance(table, dict):
            raise ValueError(f"{path}: prices: {printable(provider)} is no provider name")
        for model, entry in table.items():
            if not MODEL.fullmatch(str(model)):
                raise ValueError(f"{path}: prices: {printable(model)} is no model name")
            check_price(entry, f"{path}: prices: {provider} {model}")
    limits = document.get("limits") or []
    if not isinstance(limits, list):
        raise ValueError(f"{path}: limits is a list")
    for entry in limits:
        if not isinstance(entry, dict) or entry.get("scope") not in ("project", "all") or \
                entry.get("period") not in PERIODS or (entry["scope"] == "project" and
                                                       not isinstance(entry.get("project"), str)):
            raise ValueError(f"{path}: a limit is {{\"scope\": project|all, \"project\": KEY, \"period\": day|month, "
                             "\"usd\": N}")
        check_usd(entry.get("usd"), f"{path}: limit")
    return {"version": 1, "prices": prices, "limits": limits}


def save_settings(document, path=None):
    path = Path(path) if path else config_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    temporary.write_text(json.dumps(document, indent=2) + "\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def settings_lock():
    return module("filelock").locked(config_path())


def builtin_prices():
    """The table shipped with sandbox-ai (prices.json): {"date", "source", "providers": {...}}."""
    try:
        document = json.loads(PRICES.read_text())
    except (OSError, ValueError, RecursionError):
        return {"date": None, "source": None, "providers": {}}
    return document if isinstance(document, dict) and isinstance(document.get("providers"), dict) else \
        {"date": None, "source": None, "providers": {}}


def price_table(settings=None):
    """The table the proxy prices with: the built-in one, your own entries over it (a model of yours replaces the
    built-in one of that name; a provider of yours is added)."""
    table = builtin_prices()
    try:
        own = (settings or load_settings())["prices"]
    except ValueError:
        own = {}
    providers = {name: dict(models) for name, models in table["providers"].items()}
    for provider, models in own.items():
        providers.setdefault(provider, {}).update({str(model).lower(): entry for model, entry in models.items()})
    return {"date": table.get("date"), "source": table.get("source"), "providers": providers}


def project_key(directory):
    """The key of the project DIRECTORY: the name of its log directory (activity.default_log_dir)."""
    return module("activity").default_log_dir(Path(directory).expanduser().resolve()).name


# -- periods -----------------------------------------------------------------------------------------------------------

def period_start(period, now=None):
    """Epoch seconds the day or month that NOW is in began (local time)."""
    moment = datetime.datetime.fromtimestamp(time.time() if now is None else now)
    start = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "month":
        start = start.replace(day=1)
    return start.timestamp()


def period_end(period, now=None):
    moment = datetime.datetime.fromtimestamp(time.time() if now is None else now)
    start = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "month":
        days = calendar.monthrange(start.year, start.month)[1]
        end = start.replace(day=1) + datetime.timedelta(days=days)
    else:
        end = start + datetime.timedelta(days=1)
    return end.timestamp()


def parse_since(value, now=None):
    """--since: Nm, Nh, Nd, Nw, today, month, or a date (YYYY-MM-DD[THH:MM]); epoch seconds."""
    now = time.time() if now is None else now
    text = str(value).strip().lower()
    match = re.fullmatch(r"(\d{1,6})([mhdw])", text)
    if match:
        return now - int(match.group(1)) * {"m": 60, "h": 3600, "d": 86400, "w": 604800}[match.group(2)]
    if text in ("today", "day"):
        return period_start("day", now)
    if text == "month":
        return period_start("month", now)
    try:
        return datetime.datetime.fromisoformat(str(value).strip()).timestamp()
    except ValueError:
        raise ValueError(f"--since {printable(value)}: a time like 30m, 12h, 7d, 2w, today, month or 2026-11-01") \
            from None


# -- the spend logs ----------------------------------------------------------------------------------------------------

def spend_files(directory):
    """{run: [paths, oldest first]} of the spend logs in DIRECTORY."""
    found = {}
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return {}
    for entry in entries:
        match = FILE.fullmatch(entry.name)
        if not match:
            continue
        with contextlib.suppress(OSError):
            if entry.is_file(follow_symlinks=False):
                found.setdefault(match.group(2), []).append((match.group(1), -int(match.group(3) or 0),
                                                             Path(entry.path)))
    return {run: [path for _, _, path in sorted(paths)] for run, paths in found.items()}


def directories(extra=()):
    """The log directories to read: every one below the state directory's logs/, and EXTRA (a running run's own
    --log-dir)."""
    found = []
    with contextlib.suppress(OSError):
        found = sorted(p for p in logs_root().iterdir() if p.is_dir() and not p.is_symlink()
                       and not p.name.startswith("."))
    for path in extra:
        path = Path(path)
        if path.is_dir() and path not in found:
            found.append(path)
    return found


def running_log_dirs():
    try:
        return [record["log_dir"] for record in module("net").running().values()
                if isinstance(record, dict) and isinstance(record.get("log_dir"), str)]
    except Exception:  # noqa: BLE001 - no registry: no other log directory
        return []


def lines_of(path):
    """The lines of a spend log (compressed by sandbox-ai gc as NAME.gz: read as it is); a damaged end is left out."""
    tail = module("jsonltail")
    try:
        stream = tail.open_compressed(path) if tail.compressed(path) else open(path, "rb")
    except OSError:
        return
    with stream, contextlib.suppress(EOFError, zlib.error, OSError):
        while True:
            line = stream.readline(LINE_LIMIT)
            if not line:
                break
            if not line.endswith(b"\n"):
                continue
            try:
                record = json.loads(line)
            except (ValueError, RecursionError, UnicodeDecodeError):
                continue
            if isinstance(record, dict) and record.get("v") == 1 and isinstance(record.get("time"), (int, float)):
                yield record


def read_run(paths, directory=None):
    """One run's spend logs PATHS: {"run" (its first line: run, project, project_key, agent, session, name,
    budgets), "requests": [...], "events": [...], "directory"}."""
    facts, requests, events = {}, [], []
    for path in paths:
        for record in lines_of(path):
            event = record.get("event")
            if event == "run":
                facts = facts or record
            elif event:
                events.append(record)
            else:
                requests.append(record)
    return {"run": facts, "requests": requests, "events": events,
            "directory": str(directory) if directory is not None else str(Path(paths[0]).parent) if paths else None}


def collect(since=None, until=None, extra=(), directories_=None, budget=READ_LIMIT):
    """Every run's spend: [read_run(...) with "id" (the run) added], of the runs with a request at or after SINCE
    (epoch seconds; None: all) - each run's requests limited to [SINCE, UNTIL). At most BUDGET bytes read, the
    newest files first."""
    runs, read = [], 0
    files = []
    for directory in (directories_ if directories_ is not None else directories(extra)):
        for run, paths in spend_files(directory).items():
            try:
                modified = max(os.stat(p).st_mtime for p in paths)
                size = sum(os.stat(p).st_size for p in paths)
            except OSError:
                continue
            if since is not None and modified < since:
                continue  # written last before SINCE: nothing of it counts
            files.append((modified, size, run, paths, directory))
    for modified, size, run, paths, directory in sorted(files, key=lambda f: f[0], reverse=True):
        if read + size > budget and runs:
            break
        read += size
        found = read_run(paths, directory)
        found["requests"] = [r for r in found["requests"]
                             if (since is None or r["time"] >= since) and (until is None or r["time"] < until)]
        found["id"] = found["run"].get("run") or run
        runs.append(found)
    return runs


def priced(record, table):
    """(USD or None, how): the request's cost as the proxy wrote it, else at today's table (a price set since); one
    counted at the fallback price (a model without a price, under a budget) at today's table once it knows the model,
    else at that price ("fallback")."""
    if record.get("billing") in ("local",):
        return 0.0, "local"
    if record.get("billing") == "subscription":
        return None, "subscription"
    usd = record.get("usd")
    paid = isinstance(usd, (int, float)) and not isinstance(usd, bool)
    if paid and not isinstance(record.get("fallback"), dict):
        return float(usd), "priced"
    rt = runtime()
    found = rt.spend_price(table, record.get("provider"), record.get("model"))
    if found:
        tokens = {key: record.get(key, 0) if isinstance(record.get(key), int) else 0 for key in TOKENS}
        cost = rt.spend_cost(tokens, found[2])
        if cost is not None:
            return cost, "repriced"
    if paid:  # counted at the fallback price under a budget (the proxy's SpendMeter), no price known since
        return float(usd), "fallback"
    return None, "unknown"


def day_of(epoch):
    return time.strftime("%Y-%m-%d", time.localtime(epoch))


def rows_of(runs, by="run", table=None):
    """The requests of RUNS summed up BY (run, project, agent, provider, model, day, billing): [{"key", "requests",
    tokens..., "usd" (of those with a price), "unknown" (requests without one), "unknown_models", "fallback" (requests
    counted at the fallback price), "fallback_models", "fallback_price" ({"in", "out"}: the highest of them),
    "subscription", "local", "first", "last", "run"...}] - the costliest first (by day: the newest first)."""
    table = table if table is not None else price_table()
    rows = {}
    for found in runs:
        facts = found["run"]
        for record in found["requests"]:
            key = {"run": found["id"], "project": facts.get("project") or facts.get("project_key") or "?",
                   "agent": facts.get("agent") or "?", "provider": record.get("provider") or "?",
                   "model": record.get("model") or "?", "day": day_of(record["time"]),
                   "billing": record.get("billing") or "api"}[by]
            row = rows.setdefault(key, {"key": key, "requests": 0, **{t: 0 for t in TOKENS}, "usd": 0.0,
                                        "unknown": 0, "unknown_models": [], "fallback": 0, "fallback_models": [],
                                        "fallback_price": None, "subscription": 0, "local": 0,
                                        "first": record["time"], "last": record["time"]})
            if by == "run":
                row.update({"name": facts.get("name"), "project": facts.get("project"), "agent": facts.get("agent"),
                            "session": facts.get("session")})
            row["requests"] += 1
            for token in TOKENS:
                value = record.get(token)
                row[token] += value if isinstance(value, int) and not isinstance(value, bool) else 0
            cost, how = priced(record, table)
            if how == "subscription":
                row["subscription"] += 1
            elif how == "local":
                row["local"] += 1
            elif cost is None:
                row["unknown"] += 1
                model = str(record.get("model") or "?")
                if model not in row["unknown_models"] and len(row["unknown_models"]) < 10:
                    row["unknown_models"].append(model)
            elif how == "fallback":
                row["fallback"] += 1
                model = str(record.get("model") or "?")
                if model not in row["fallback_models"] and len(row["fallback_models"]) < 10:
                    row["fallback_models"].append(model)
                row["fallback_price"] = higher_price(row["fallback_price"], record["fallback"])
            if cost:
                row["usd"] += cost
            row["first"], row["last"] = min(row["first"], record["time"]), max(row["last"], record["time"])
    for row in rows.values():
        row["usd"] = round(row["usd"], 6)
    order = sorted(rows.values(), key=lambda r: r["key"], reverse=True) if by == "day" else \
        sorted(rows.values(), key=lambda r: (-r["usd"], -r["last"]))
    return order


def higher_price(first, second):
    """{"in", "out"}: the higher of each of two fallback prices (either None or not one)."""
    found = {}
    for price in (first, second):
        for key in ("in", "out"):
            value = price.get(key) if isinstance(price, dict) else None
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                found[key] = max(found.get(key, 0.0), float(value))
    return found or None


def fallback_text(rows):
    """None, or how many requests were counted at the fallback price, at which price, for which models: "N
    request(s) priced at the fallback price USD X in / USD Y out per million tokens", [models]."""
    count = sum(row.get("fallback") or 0 for row in rows)
    if not count:
        return None, []
    price = None
    for row in rows:
        price = higher_price(price, row.get("fallback_price"))
    price = price or {}
    models = sorted({m for row in rows for m in row.get("fallback_models") or []})
    return (f"{count} request(s) priced at the fallback price USD {price.get('in', 0):.2f} in / USD "
            f"{price.get('out', 0):.2f} out per million tokens"), models


def totals(rows):
    total = {"requests": 0, **{t: 0 for t in TOKENS}, "usd": 0.0, "unknown": 0, "fallback": 0, "subscription": 0,
             "local": 0}
    for row in rows:
        for key in total:
            total[key] += row.get(key) or 0
    total["usd"] = round(total["usd"], 6)
    return total


def cost_text(row):
    """What a row cost, in words: "USD 1.23", "+ 2 of unknown cost", "subscription", "0 (local)"."""
    parts = []
    paid = row["requests"] - row["subscription"] - row["local"]
    if row["usd"] or paid - row["unknown"] > 0:
        parts.append(f"USD {row['usd']:.4f}" if 0 < row["usd"] < 0.01 else f"USD {row['usd']:.2f}")
    if row["unknown"]:
        parts.append(f"{'+ ' if parts else ''}{row['unknown']} unknown")
    if row["subscription"]:
        parts.append("subscription" if not parts else "+ subscription")
    if row["local"] and not parts:
        parts.append("0 (local)")
    return " ".join(parts) or "USD 0.00"


def tokens_text(number):
    return f"{number / 1e6:.1f}M" if number >= 1e6 else f"{number / 1e3:.1f}k" if number >= 1e4 else str(number)


# -- budgets -----------------------------------------------------------------------------------------------------------

def limits(settings=None):
    """Your limits ([{"scope", "project", "path", "period", "usd"}])."""
    return list((settings or load_settings())["limits"])


def limit_id(entry):
    return f"all-{entry['period']}" if entry["scope"] == "all" else f"project-{entry['period']}:{entry['project']}"


def limit_label(entry):
    if entry["scope"] == "all":
        return f"{entry['period']} limit of all projects"
    name = Path(entry.get("path") or entry["project"]).name or entry["project"]
    return f"{entry['period']} limit of the project {name}"


def spent_in(runs, scope_key=None, exclude=None, table=None):
    """USD of RUNS' requests (those of the project SCOPE_KEY only, when given; not the run EXCLUDE)."""
    total = 0.0
    table = table if table is not None else price_table()
    for found in runs:
        if exclude is not None and found["id"] == exclude:
            continue
        if scope_key is not None and found["run"].get("project_key") != scope_key:
            continue
        for record in found["requests"]:
            cost, _ = priced(record, table)
            total += cost or 0.0
    return round(total, 6)


def start_budgets(key, run_budget=None, now=None, settings=None, extra=None):
    """The budgets of a run that starts now in the project KEY (proxy.json's "spend" "budgets"): RUN_BUDGET (USD,
    --budget) and every limit of yours that holds for it, each with what its period used before the run (EXTRA: log
    directories beside the state directory's; None: the running runs' own, --log-dir)."""
    now = time.time() if now is None else now
    extra = running_log_dirs() if extra is None else extra
    settings = settings or load_settings()
    table = price_table(settings)
    budgets = []
    if run_budget is not None:
        budgets.append({"id": "run", "scope": "run", "period": None, "usd": float(run_budget), "used": 0.0,
                        "until": None, "label": "budget of this run (--budget)"})
    cache = {}
    for entry in limits(settings):
        if entry["scope"] == "project" and entry["project"] != key:
            continue
        since = period_start(entry["period"], now)
        if since not in cache:
            cache[since] = collect(since=since, until=now, extra=extra)
        used = spent_in(cache[since], entry["project"] if entry["scope"] == "project" else None, table=table)
        budgets.append({"id": limit_id(entry), "scope": entry["scope"], "period": entry["period"],
                        "usd": float(entry["usd"]), "used": used, "until": period_end(entry["period"], now),
                        "label": limit_label(entry)})
    return budgets


def proxy_config(run, key, project, agent, session=None, name=None, budget=None, now=None, extra=None):
    """proxy.json's "spend" for a run: what the proxy prices with and the budgets it holds the run to."""
    settings = load_settings()
    return {"run": run, "project": project, "project_key": key, "agent": agent, "session": session, "name": name,
            "prices": {"providers": price_table(settings)["providers"]},
            "budgets": start_budgets(key, budget, now, settings, extra)}


def run_state(found, table=None):
    """One run's spend (read_run with "id") as the app and `spend run` show it: {"run", "name", "project", "agent",
    "session", "requests", tokens, "usd", "unknown", "unknown_models", "fallback" (requests counted at the fallback
    price: a model without a price, under a budget), "fallback_models", "fallback_price", "subscription", "local",
    "raised", "refused", "budgets": [{"id",
    "label", "usd" (allowed: with the raise), "used", "share"}]}."""
    table = table if table is not None else price_table()
    rows = rows_of([found], "run", table)
    total = rows[0] if rows else {"requests": 0, **{t: 0 for t in TOKENS}, "usd": 0.0, "unknown": 0,
                                  "unknown_models": [], "fallback": 0, "fallback_models": [], "fallback_price": None,
                                  "subscription": 0, "local": 0}
    facts = found["run"]
    raised, refused, latest = 0.0, False, {}
    for event in found["events"]:
        if event.get("event") == "raised" and isinstance(event.get("raised"), (int, float)):
            raised, refused = float(event["raised"]), False
        elif event.get("event") == "refused":
            refused = True
        if event.get("event") in ("budget", "refused") and isinstance(event.get("used"), (int, float)):
            latest[event.get("budget")] = float(event["used"])
    budgets = []
    for budget in facts.get("budgets") or []:
        if not isinstance(budget, dict) or not isinstance(budget.get("usd"), (int, float)):
            continue
        before = budget.get("used") if isinstance(budget.get("used"), (int, float)) else 0.0
        used = round(max(before + total["usd"], latest.get(budget.get("id"), 0.0)), 6)
        allowed = round(budget["usd"] + raised, 6)
        budgets.append({"id": budget.get("id"), "label": budget.get("label") or budget.get("id"),
                        "usd": allowed, "used": used,
                        "share": round(used / allowed, 4) if allowed > 0 else (1.0 if used > 0 else 0.0)})
    return {"run": found["id"], "name": facts.get("name"), "project": facts.get("project"),
            "agent": facts.get("agent"), "session": facts.get("session"), "requests": total["requests"],
            **{t: total[t] for t in TOKENS}, "usd": total["usd"], "unknown": total["unknown"],
            "unknown_models": total["unknown_models"], "fallback": total["fallback"],
            "fallback_models": total["fallback_models"], "fallback_price": total["fallback_price"],
            "subscription": total["subscription"], "local": total["local"],
            "raised": raised, "refused": refused, "budgets": budgets, "directory": found.get("directory")}


def find_run(name):
    """The spend of the run NAME (its run id, or the name of a running start): read_run with "id"; ValueError when
    there is no spend log of it."""
    record, extra = None, []
    with contextlib.suppress(Exception):
        net = module("net")
        runs = net.running()
        with contextlib.suppress(Exception):
            record = net.choose(runs, name, command="spend")
        extra = [r["log_dir"] for r in runs.values() if isinstance(r, dict) and isinstance(r.get("log_dir"), str)]
    run = record.get("run") if isinstance(record, dict) else name
    places = [Path(record["log_dir"])] if isinstance(record, dict) and isinstance(record.get("log_dir"), str) else []
    for directory in places + directories(extra):
        files = spend_files(directory)
        if run in files:
            found = read_run(files[run], directory)
            found["id"] = run
            found["running"] = record is not None
            return found
    raise ValueError(f"no spend log of a run {printable(name)!r} (it is written while a run with the connection proxy "
                     "makes model requests)")


def raise_budget(name, usd):
    """`sandbox-ai spend raise`: the running run NAME may spend USD more than its budgets."""
    amount = check_usd(usd, "raise")
    if amount <= 0:
        raise ValueError("raise: by more than 0 USD")
    net = module("net")
    record = net.choose(net.running(), name, command="spend raise")
    answer = net.ask(record["directory"], {"op": "spend", "raise": amount})
    if not isinstance(answer, dict) or not answer.get("ok"):
        raise ValueError(str((answer or {}).get("error") or "the sandbox refused"))
    return {"run": record.get("run"), "raised": answer.get("raised")}


# -- the launcher's watch ----------------------------------------------------------------------------------------------

class SpendWatch:
    """In the launcher while a run goes: the proxy's budget warnings (80 %, 100 %) to the human (NOTIFY(title, text,
    record)), and, for a day's or month's limit, what other runs spent of it since this one started, to the proxy
    (TELL({budget id: USD}): control op "spend")."""

    def __init__(self, path, run, budgets, notify, tell=None, started=None, clock=time.time, extra=()):
        self.path, self.run, self.budgets = Path(path), run, [b for b in budgets or [] if isinstance(b, dict)]
        self.notify, self.tell, self.clock, self.extra = notify, tell, clock, list(extra)
        self.started = clock() if started is None else started
        self.offset, self.pending, self.sent, self.told_at = 0, b"", None, 0.0
        self.used_up = set()  # the budgets said to be used up (a refusal after it is not said again)
        self.stop_event, self.thread = threading.Event(), None

    def look(self):
        """New lines of the spend log: each budget warning told."""
        try:
            with open(self.path, "rb") as stream:
                stream.seek(self.offset)
                data = stream.read(1 << 20)
        except OSError:
            return []
        self.offset += len(data)
        self.pending += data
        *lines, self.pending = self.pending.split(b"\n")
        told = []
        for line in lines:
            try:
                record = json.loads(line)
            except (ValueError, RecursionError, UnicodeDecodeError):
                continue
            if isinstance(record, dict) and record.get("event") in ("budget", "refused"):
                used_up = record.get("event") == "refused" or (record.get("level") or 0) >= 100
                if record.get("event") == "refused" and record.get("budget") in self.used_up:
                    continue  # said already when it was used up
                if used_up:
                    self.used_up.add(record.get("budget"))
                told.append(record)
                with contextlib.suppress(Exception):
                    self.notify(*warning_text(record), record)
            elif isinstance(record, dict) and record.get("event") == "raised":
                self.used_up.clear()
            elif isinstance(record, dict) and record.get("event") == "fallback":
                told.append(record)
                with contextlib.suppress(Exception):
                    self.notify(*warning_text(record), record)
        return told

    def others(self):
        """{budget id: USD} other runs spent of each day's or month's limit since this run started (its period's
        start, if later)."""
        shared = [b for b in self.budgets if b.get("scope") in ("project", "all")]
        if not shared:
            return {}
        now = self.clock()
        result = {}
        table = price_table()
        for budget in shared:
            since = max(self.started, period_start(budget.get("period") or "day", now))
            runs = collect(since=since, until=now, extra=self.extra or running_log_dirs())
            key = budget["id"].split(":", 1)[1] if budget.get("scope") == "project" else None
            result[budget["id"]] = spent_in(runs, key, exclude=self.run, table=table)
        return result

    def step(self):
        told = self.look()
        if self.tell is not None and self.clock() - self.told_at >= OTHERS_SECONDS:
            self.told_at = self.clock()
            with contextlib.suppress(Exception):
                others = self.others()
                if others and others != self.sent:
                    self.tell(others)
                    self.sent = others
        return told

    def serve(self):
        while not self.stop_event.wait(WATCH_SECONDS):
            with contextlib.suppress(Exception):
                self.step()
        with contextlib.suppress(Exception):
            self.look()  # the last warnings of the run

    def start(self):
        self.thread = threading.Thread(target=self.serve, daemon=True, name="sandbox-ai-spend")
        self.thread.start()
        return self

    def stop(self):
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(5)


def warning_text(record):
    """(title, text) of a budget warning of the proxy (a "budget", "refused" or "fallback" line of the spend log)."""
    if record.get("event") == "fallback":
        price = record.get("price") if isinstance(record.get("price"), dict) else {}
        model = printable(record.get("model") or "?")
        return ("sandbox-ai: a model without a price",
                f"{model} has no price in the table: counted at the fallback price USD {price.get('in') or 0:.2f} in "
                f"/ USD {price.get('out') or 0:.2f} out per million tokens for the budget; set one: sandbox-ai spend "
                f"prices set {model} in=USD out=USD")
    label = record.get("label") or record.get("budget") or "budget"
    used, usd = record.get("used") or 0, record.get("usd") or 0
    if record.get("event") == "refused" or record.get("level", 0) >= 100:
        return ("sandbox-ai: budget used up",
                f"the {label} is used up (USD {used:.2f} of {usd:.2f}): the run's next model requests are refused; "
                "raise it with sandbox-ai spend raise RUN USD")
    return ("sandbox-ai: budget nearly used up",
            f"the {label} is {record.get('level', 80)} % used (USD {used:.2f} of {usd:.2f})")


# -- the command -------------------------------------------------------------------------------------------------------

def help_style(prog):
    loaded = sys.modules.get("sandbox_ai_helpstyle")
    if loaded is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_helpstyle", HERE / "helpstyle.py")
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        sys.modules["sandbox_ai_helpstyle"] = loaded
    return loaded.CommandsFormatter(prog)


def parser():
    p = argparse.ArgumentParser(prog="sandbox-ai spend", description=__doc__.split("\n\n")[0],
                                formatter_class=help_style, allow_abbrev=False,
                                epilog="\n\n".join(__doc__.split("\n\n")[2:4]))
    p.add_argument("--by", choices=BY, default="run", help="Sum up by run (default), project, agent, provider, "
                                                           "model, day or billing")
    p.add_argument("--since", metavar="WHEN", help="Only requests since then: 30m, 12h, 7d, 2w, today, month or a "
                                                   "date (default: all the logs hold)")
    p.add_argument("--project", metavar="DIR", help="Only the runs of this project (a directory)")
    p.add_argument("--run", metavar="RUN", help="Only this run (its run id)")
    p.add_argument("--json", action="store_true", help="Print the result as JSON (for programs)")
    commands = p.add_subparsers(dest="command")
    shown = commands.add_parser("run", help="one run: its cost so far and its budgets")
    shown.add_argument("name", metavar="RUN", help="the run (its id, or the name of a running start)")
    shown.add_argument("--json", action="store_true", help="Print the result as JSON (for programs)")
    raised = commands.add_parser("raise", help="let a running run spend more than its budgets")
    raised.add_argument("name", metavar="RUN", help="the running run (its id, or the name of its start)")
    raised.add_argument("usd", metavar="USD", help="how many USD more it may spend")
    prices = commands.add_parser("prices", help="the price table (USD per million tokens); set your own")
    prices.add_argument("--json", action="store_true", help="Print the result as JSON (for programs)")
    price_commands = prices.add_subparsers(dest="action")
    price_set = price_commands.add_parser("set", help="your price of a model (replaces the built-in one)")
    price_set.add_argument("model", metavar="MODEL", help="the model's name, or the start of its names")
    price_set.add_argument("values", nargs="+", metavar="KEY=USD",
                           help="in=, out= (needed), cached=, write=, write_1h= per million tokens")
    price_set.add_argument("--provider", metavar="NAME", help="The provider (default: the one whose table knows "
                                                              "the model, else custom)")
    price_remove = price_commands.add_parser("remove", help="forget your price of a model")
    price_remove.add_argument("model", metavar="MODEL")
    price_remove.add_argument("--provider", metavar="NAME", help="The provider (default: every one)")
    limit = commands.add_parser("limit", help="limits per day or month, for a project or everything")
    limit.add_argument("--json", action="store_true", help="Print the result as JSON (for programs)")
    limit_commands = limit.add_subparsers(dest="action")
    for action, text in (("set", "set a limit"), ("remove", "remove a limit")):
        sub = limit_commands.add_parser(action, help=text)
        scope = sub.add_mutually_exclusive_group(required=True)
        scope.add_argument("--project", metavar="DIR", nargs="?", const=".",
                           help="The project DIR's runs (default: this directory)")
        scope.add_argument("--all", action="store_true", help="Every run together")
        period = sub.add_mutually_exclusive_group(required=True)
        period.add_argument("--day", action="store_true", help="Per day (local time)")
        period.add_argument("--month", action="store_true", help="Per calendar month")
        if action == "set":
            sub.add_argument("usd", metavar="USD", help="the limit in USD")
    return p


def run_listing(args, out):
    since = parse_since(args.since) if args.since else None
    table = price_table()
    runs = collect(since=since, extra=running_log_dirs())
    if args.project:
        key = project_key(args.project)
        runs = [r for r in runs if r["run"].get("project_key") == key]
    if args.run:
        runs = [r for r in runs if r["id"] == args.run]
    rows = rows_of(runs, args.by, table)
    total = totals(rows)
    if args.json:
        out(json.dumps({"by": args.by, "since": since, "rows": rows, "total": total,
                        "prices": {"date": table.get("date")}}, indent=2))
        return 0
    if not rows:
        out("no model requests counted" + (" in that time" if since else "") + " (the proxy counts them while a run "
            "makes model requests)")
        return 0
    head = args.by.upper()
    width = max([len(head)] + [len(printable(r["key"])) for r in rows[:200]])
    width = min(width, 48)
    out(f"{head:<{width}}  {'REQUESTS':>8}  {'INPUT':>7}  {'CACHED':>7}  {'WRITTEN':>7}  {'OUTPUT':>7}  COST")
    for row in rows[:200]:
        out(f"{printable(row['key'])[:width]:<{width}}  {row['requests']:>8}  {tokens_text(row['in']):>7}  "
            f"{tokens_text(row['cached']):>7}  {tokens_text(row['write']):>7}  {tokens_text(row['out']):>7}  "
            f"{cost_text(row)}")
    if len(rows) > 200:
        out(f"... and {len(rows) - 200} more (--json lists them all)")
    out(f"{'total':<{width}}  {total['requests']:>8}  {tokens_text(total['in']):>7}  {tokens_text(total['cached']):>7}"
        f"  {tokens_text(total['write']):>7}  {tokens_text(total['out']):>7}  {cost_text(total)}")
    unknown = sorted({m for r in rows for m in r["unknown_models"]})
    if unknown:
        out(f"note: {total['unknown']} request(s) of unknown cost - no price for {', '.join(map(printable, unknown))}; "
            "set one: sandbox-ai spend prices set MODEL in=USD out=USD")
    said, models = fallback_text(rows)
    if said:
        out(f"note: {said} (no price known for {', '.join(map(printable, models))}) - a run with a budget counts "
            "such a model so; set one: sandbox-ai spend prices set MODEL in=USD out=USD")
    if total["subscription"]:
        out(f"note: {total['subscription']} request(s) on a subscription (Claude Pro/Max, a ChatGPT plan): counted in "
            "tokens, no price per token")
    return 0


def bar(share, width=20):
    filled = min(width, int(round(min(share, 1.0) * width)))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def show_run(args, out):
    state = run_state(find_run(args.name))
    if args.json:
        out(json.dumps(state, indent=2))
        return 0
    row = {**state, "unknown_models": state["unknown_models"]}
    out(f"{printable(state['name'] or state['run'])}: {cost_text(row)} - {state['requests']} model requests, "
        f"input {tokens_text(state['in'])}, cached {tokens_text(state['cached'])}, written "
        f"{tokens_text(state['write'])}, output {tokens_text(state['out'])}"
        + (f" (reasoning {tokens_text(state['reasoning'])})" if state["reasoning"] else ""))
    for budget in state["budgets"]:
        out(f"  {bar(budget['share'])} {round(budget['share'] * 100)} % of USD {budget['usd']:.2f} - "
            f"{budget['label']}")
    if state["raised"]:
        out(f"  raised by USD {state['raised']:.2f} during the run")
    if state["refused"]:
        out(f"  refused model requests: a budget is used up (sandbox-ai spend raise {state['run']} USD)")
    if not state["budgets"]:
        out("  no budget (--budget USD, sandbox-ai spend limit set)")
    if state["unknown"]:
        out(f"  {state['unknown']} request(s) of unknown cost: no price for "
            f"{', '.join(map(printable, state['unknown_models']))}")
    said, models = fallback_text([state])
    if said:
        out(f"  {said}: no price known for {', '.join(map(printable, models))} (set one: sandbox-ai spend prices "
            "set MODEL in=USD out=USD)")
    return 0


def parse_values(values):
    entry = {}
    for value in values:
        key, equals, number = value.partition("=")
        if not equals or key not in PRICE_KEYS:
            raise ValueError(f"{printable(value)}: KEY=USD with KEY one of {', '.join(PRICE_KEYS)}")
        entry[key] = check_usd(number, key)
    return check_price(entry, "prices set")


def prices_command(args, out):
    if args.action is None:
        table = price_table()
        own = load_settings()["prices"]
        if args.json:
            out(json.dumps({**table, "own": own}, indent=2))
            return 0
        out(f"USD per million tokens - built in as of {table.get('date') or '?'} (yours marked *); "
            "in / cached / written / out")
        for provider, models in sorted(table["providers"].items()):
            out(f"{provider}:")
            for model, entry in sorted(models.items()):
                mark = "*" if str(model).lower() in {str(m).lower() for m in own.get(provider, {})} else " "
                long = f"  (above {entry['long']['above']} tokens: in {entry['long'].get('in')}, out " \
                    f"{entry['long'].get('out')})" if isinstance(entry.get("long"), dict) else ""
                out(f" {mark}{model:<28} {entry.get('in')} / {entry.get('cached', entry.get('in'))} / "
                    f"{entry.get('write', '-')} / {entry.get('out')}{long}")
        out(f"source: {table.get('source') or '?'}")
        return 0
    model = args.model.strip().lower()
    if not MODEL.fullmatch(model):
        raise ValueError(f"{printable(args.model)} is no model name")
    if args.provider is not None and not PROVIDER.fullmatch(args.provider):
        raise ValueError(f"{printable(args.provider)} is no provider name (lower-case letters, digits, _ . : -)")
    with settings_lock():
        settings = load_settings()
        if args.action == "set":
            entry = parse_values(args.values)
            provider = args.provider
            if provider is None:
                found = runtime().spend_price(builtin_prices(), None, model)
                provider = found[0] if found else "custom"
            settings["prices"].setdefault(provider, {})[model] = entry
            save_settings(settings)
            out(f"{provider} {model}: in {entry['in']}, out {entry['out']}"
                + "".join(f", {k} {entry[k]}" for k in ("cached", "write", "write_1h") if k in entry)
                + " USD per million tokens (for the runs that start from now on)")
            return 0
        removed = []
        for provider, models in settings["prices"].items():
            if args.provider not in (None, provider):
                continue
            if model in models:
                del models[model]
                removed.append(provider)
        if not removed:
            raise ValueError(f"no price of yours for {printable(model)}")
        settings["prices"] = {p: m for p, m in settings["prices"].items() if m}
        save_settings(settings)
        out(f"{model}: your price removed ({', '.join(removed)}); the built-in one holds again, if there is one")
        return 0


def limit_command(args, out):
    if args.action is None:
        listed = limits()
        if args.json:
            out(json.dumps({"limits": [{**entry, "id": limit_id(entry), "label": limit_label(entry)}
                                       for entry in listed]}, indent=2))
            return 0
        if not listed:
            out("no limits; set one, e.g.: sandbox-ai spend limit set --project . --day 10")
        for entry in listed:
            out(f"USD {entry['usd']:.2f} - {limit_label(entry)}"
                + (f" ({printable(entry.get('path') or entry['project'])})" if entry["scope"] == "project" else ""))
        return 0
    period = "day" if args.day else "month"
    if args.all:
        wanted = {"scope": "all", "period": period}
    else:
        directory = Path(args.project).expanduser().resolve()
        wanted = {"scope": "project", "project": project_key(directory), "path": str(directory), "period": period}
    with settings_lock():
        settings = load_settings()
        same = [e for e in settings["limits"] if limit_id(e) == limit_id(wanted)]
        kept = [e for e in settings["limits"] if limit_id(e) != limit_id(wanted)]
        if args.action == "set":
            wanted["usd"] = check_usd(args.usd, "limit")
            settings["limits"] = kept + [wanted]
            save_settings(settings)
            out(f"USD {wanted['usd']:.2f} - {limit_label(wanted)} (for the runs that start from now on)")
            return 0
        if not same:
            raise ValueError(f"no {limit_label(wanted)}")
        settings["limits"] = kept
        save_settings(settings)
        out(f"removed: the {limit_label(wanted)}")
        return 0


def main(argv=None, out=print):
    args = parser().parse_args(argv)
    try:
        if args.command == "run":
            return show_run(args, out)
        if args.command == "raise":
            done = raise_budget(args.name, args.usd)
            out(f"{printable(done['run'])}: may spend USD {float(done['raised'] or 0):.2f} more than its budgets "
                "(in all, this run); its next model request goes")
            return 0
        if args.command == "prices":
            return prices_command(args, out)
        if args.command == "limit":
            return limit_command(args, out)
        return run_listing(args, out)
    except ValueError as exc:
        print(f"sandbox-ai spend: {printable(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
