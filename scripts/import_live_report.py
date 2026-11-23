#!/usr/bin/env python3
"""The live check's report (scripts/live_check.sh: live-check/REPORT-DATE.json) into sandbox/live_evidence.json: what
presets-live and harnesses-live found per preset or harness and backend, the evidence the presets' lights and the
harnesses' live results are shown from.

  python3 scripts/import_live_report.py live-check/REPORT-20261123-091500.json [--dry-run] [--evidence FILE]
  python3 scripts/import_live_report.py --check [--evidence FILE]

Each run that passed or failed becomes an entry {kind (preset or harness), name, backend (podman or bwrap), agent,
result (passed or failed), date, version (the sandbox-ai that ran it), report (the report's id and commit), detail
(how long it took, or the reason it failed)}; a newer report's entry replaces the older one of the same kind, name and
backend. A run that was skipped or not run is no evidence: nothing is imported for it, and an earlier entry stays.
--check validates the file (what tests/test_live_evidence.py does). The report is redacted already (secrets and home
paths replaced by the live check); nothing else of it is kept. Standard library only.
"""
import argparse
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "sandbox" / "live_evidence.json"
SCHEMA = 1
KINDS = ("preset", "harness")
BACKENDS = ("podman", "bwrap")
RESULTS = ("passed", "failed")
KEYS = {"kind", "name", "backend", "agent", "result", "date", "version", "report", "detail"}
NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
VERSION = re.compile(r"\d+\.\d+\.\d+(?:[.+-][A-Za-z0-9.]+)?")
STAMP = re.compile(r"(\d{4})(\d{2})(\d{2})-\d{6}")


def empty():
    return {"schema": SCHEMA, "made_by": "scripts/import_live_report.py", "entries": []}


def check(document, where="live evidence"):
    """DOCUMENT (live_evidence.json) checked whole; ValueError names the first fault."""
    if not isinstance(document, dict) or document.get("schema") != SCHEMA or \
            not isinstance(document.get("entries"), list):
        raise ValueError(f"{where}: schema {SCHEMA} with entries")
    seen = set()
    for index, entry in enumerate(document["entries"]):
        at = f"{where}: entry {index}"
        if not isinstance(entry, dict) or set(entry) - KEYS or not {"kind", "name", "backend", "result", "date",
                                                                    "version", "report"} <= set(entry):
            raise ValueError(f"{at}: an object of {', '.join(sorted(KEYS))}")
        if entry["kind"] not in KINDS or entry["backend"] not in BACKENDS or entry["result"] not in RESULTS:
            raise ValueError(f"{at}: kind {'/'.join(KINDS)}, backend {'/'.join(BACKENDS)}, result "
                             f"{'/'.join(RESULTS)}")
        if not isinstance(entry["name"], str) or not NAME.fullmatch(entry["name"]):
            raise ValueError(f"{at}: name is a preset's or harness's id")
        if not isinstance(entry["date"], str) or not DATE.fullmatch(entry["date"]):
            raise ValueError(f"{at}: date YYYY-MM-DD")
        if not isinstance(entry["version"], str) or not VERSION.fullmatch(entry["version"]):
            raise ValueError(f"{at}: version is sandbox-ai's (0.1.212)")
        for key in ("report", "detail", "agent"):
            value = entry.get(key, "")
            if not isinstance(value, str) or len(value) > 400 or not value.isprintable():
                raise ValueError(f"{at}: {key} is a short line")
        if not entry["report"]:
            raise ValueError(f"{at}: report names the live check's report it comes from")
        key = (entry["kind"], entry["name"], entry["backend"])
        if key in seen:
            raise ValueError(f"{at}: a second entry for {'/'.join(key)}")
        seen.add(key)
    return document


def load(path=EVIDENCE):
    try:
        return check(json.loads(Path(path).read_text()), str(path))
    except FileNotFoundError:
        return empty()


def outcome_of(cell):
    """(result, detail) of a report's cell ("started (12 s)", "passed (40 s)", "failed: why"), or None for one that
    is no evidence (skipped, not run)."""
    text = str(cell or "").strip()
    for word, result in (("started", "passed"), ("passed", "passed"), ("failed", "failed"), ("error", "failed")):
        if text == word or text.startswith(word + " ") or text.startswith(word + ":"):
            detail = text[len(word):].strip().lstrip(":").strip().strip("()")
            return result, detail[:400]
    return None


def report_facts(report):
    """(date YYYY-MM-DD, version, report id) of a live-check REPORT; ValueError when it lacks them."""
    facts = report.get("facts") if isinstance(report, dict) else None
    if not isinstance(facts, dict):
        raise ValueError("not a live-check report (no facts)")
    stamp = STAMP.fullmatch(str(report.get("date") or facts.get("date") or ""))
    version = VERSION.search(str(facts.get("sandbox-ai") or ""))
    if not stamp or not version:
        raise ValueError("the report names no date or no sandbox-ai version (facts)")
    commit = facts.get("commit")
    ident = f"REPORT-{report.get('date') or facts.get('date')}" + (f" (commit {commit})" if commit else "")
    return f"{stamp.group(1)}-{stamp.group(2)}-{stamp.group(3)}", version.group(0), ident


def entries_of(report):
    """The evidence REPORT holds: [entry] of presets-live's rows and harnesses-live's (passed or failed only)."""
    date, version, ident = report_facts(report)
    found = []
    for check_entry in report.get("checks") or []:
        if not isinstance(check_entry, dict):
            continue
        rows, kind, name_key = ((check_entry.get("presets"), "preset", "preset")
                                if check_entry.get("name") == "presets-live" else
                                (check_entry.get("harnesses"), "harness", "agent")
                                if check_entry.get("name") == "harnesses-live" else (None, None, None))
        for row in rows or []:
            if not isinstance(row, dict) or not NAME.fullmatch(str(row.get(name_key) or "")):
                continue
            for backend in BACKENDS:
                outcome = outcome_of(row.get(backend))
                if outcome is None:
                    continue
                result, detail = outcome
                if kind == "preset" and result == "passed" and str(row.get("part", "")).startswith("came up: "):
                    detail = ", ".join(filter(None, [detail, row["part"]]))[:400]
                found.append({"kind": kind, "name": row[name_key], "backend": backend,
                              "agent": str(row.get("agent") or (row[name_key] if kind == "harness" else "")),
                              "result": result, "date": date, "version": version, "report": ident,
                              "detail": "".join(c for c in detail if c.isprintable())})
    return found


def merged(document, new):
    """DOCUMENT with NEW's entries: each replaces the one of its kind, name and backend when it is as new or newer;
    (the document, [(entry, "added" | "replaced" | "kept older")])."""
    entries = {(e["kind"], e["name"], e["backend"]): e for e in document["entries"]}
    said = []
    for entry in new:
        key = (entry["kind"], entry["name"], entry["backend"])
        old = entries.get(key)
        if old is not None and old["date"] > entry["date"]:
            said.append((entry, "kept the newer one"))
            continue
        entries[key] = entry
        said.append((entry, "replaced" if old else "added"))
    document = {**document, "entries": sorted(entries.values(), key=lambda e: (e["kind"], e["name"], e["backend"]))}
    return check(document), said


def main(argv=None, out=print):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("report", nargs="?", type=Path, help="a live check's REPORT-DATE.json")
    parser.add_argument("--evidence", type=Path, default=EVIDENCE, help="live_evidence.json (default: sandbox/'s)")
    parser.add_argument("--dry-run", action="store_true", help="say what would change, write nothing")
    parser.add_argument("--check", action="store_true", help="only check the evidence file")
    args = parser.parse_args(argv)
    try:
        document = load(args.evidence)
        if args.check:
            out(f"{args.evidence}: {len(document['entries'])} entries, valid")
            return 0
        if args.report is None:
            parser.error("a report (or --check)")
        report = json.loads(args.report.read_text())
        new = entries_of(report)
        document, said = merged(document, new)
    except (OSError, ValueError) as exc:
        out(f"import_live_report: {exc}")
        return 1
    for entry, what in said:
        out(f"{what}: {entry['kind']} {entry['name']} ({entry['backend']}): {entry['result']}"
            + (f" - {entry['detail']}" if entry["detail"] else ""))
    if not said:
        out("nothing to import: the report has no presets-live or harnesses-live run that passed or failed")
    if not args.dry_run and said:
        args.evidence.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n")
        out(f"wrote {args.evidence} ({len(document['entries'])} entries)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
