#!/usr/bin/env python3
"""The model x harness statuses of sandbox/model_profiles.json, derived from the local-model research's graded runs.

  python3 scripts/profile_status.py --research DIR [--last-round N] [--date "round 7=2026-11-20"] [--write]
                                    [--summary FILE]
  python3 scripts/profile_status.py --check

--research DIR is the research tree (untracked; its path never goes into the repository): phase2/ and round1/ ...
round4/ with their results.jsonl (pass/fail of their tasks), round5/results.jsonl (rounds 5 on: round 5's graded
use-case tasks; the last line of a label wins, as after a regrade), round5/queue.log (which plan - plan5_*, plan6_*,
plan7_* - ran a label, so its round) and round5/contaminated.json (runs that read the research's own files).
--last-round N reads rounds up to N (phase 2 and round 1 on). The rules are sandbox/model_evidence.py's (RULES;
docs/LOCAL_MODELS.md "How a status is derived").

Without --write it says what would change. It writes nothing when a pair it derived before has no data in this
run (a lower --last-round, an incomplete tree): table and snapshot would no longer agree. With it, it writes each
measured pair's status, its evidence block and runs/solved into the table (every other key of the table kept: the hand-written settings, notes, works/fails, the
evidence text as the block's summary) and the aggregated numbers into sandbox/model_evidence.json - the snapshot
tests/test_profile_status.py checks the table against without the research tree. --summary FILE writes what changed
and why (Markdown). --check compares the table with the snapshot alone (exit 1 on a contradiction, and on any status
the snapshot's runs do not back: a family's own status, a pair's without its run ids - a light comes only from a
measured pair).

After a round: run it again with that round (--last-round N or none: every round found) and --date "round N=DATE"
when the table's "rounds" has no date for it yet, review the summary, run the tests, commit table and snapshot.
Standard library only.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
TABLE = ROOT / "sandbox" / "model_profiles.json"
SNAPSHOT = ROOT / "sandbox" / "model_evidence.json"
LEGACY = (("phase 2", "phase2"), ("round 1", "round1"), ("round 2", "round2"), ("round 3", "round3"),
          ("round 4", "round4"))
CURRENT = "round5"  # rounds 5 on: one results file, one queue log
PLAN = re.compile(r"chain: start \S*?plan(\d+)_")
RUN = re.compile(r"RUN \[([^\]]+)\]")


def load(name):
    spec = importlib.util.spec_from_file_location(f"profile_status_{name}", ROOT / "sandbox" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ev = load("model_evidence")
mp = load("model_profiles")


def lines(path):
    """The JSON lines of PATH, the last line of each label winning (a regrade), in the order of their first line."""
    found = {}
    with open(path, encoding="utf-8") as handle:
        for text in handle:
            if text.strip():
                row = json.loads(text)
                found[row["label"]] = row
    return list(found.values())


def rounds_of(queue):
    """{label: "round N"} from the queue log QUEUE: the plan (planN_...) that ran it."""
    found, current = {}, None
    with open(queue, encoding="utf-8") as handle:
        for text in handle:
            plan = PLAN.search(text)
            if plan:
                current = f"round {int(plan.group(1))}"
                continue
            run = RUN.search(text)
            if run and current:
                found[run.group(1)] = current
    return found


def read(research, last=None, out=print):
    """Every run of the research tree RESEARCH up to round LAST (None: all) as normalize()'s runs, and the rounds
    read."""
    research = Path(research)
    runs, rounds = [], []
    for name, folder in LEGACY:
        number = ev.round_key(name)[1] if name.startswith("round") else 0
        if last is not None and number > last:
            continue
        rounds.append(name)
        path = research / folder / "results.jsonl"
        if path.exists():
            runs += [ev.normalize(row, name) for row in lines(path)]
    current = research / CURRENT
    if (current / "results.jsonl").exists():
        where = rounds_of(current / "queue.log") if (current / "queue.log").exists() else {}
        contaminated = set(json.loads((current / "contaminated.json").read_text())) \
            if (current / "contaminated.json").exists() else set()
        unplaced = 0
        for row in lines(current / "results.jsonl"):
            name = where.get(row["label"])
            if name is None:
                unplaced += 1
                continue
            if last is not None and ev.round_key(name)[1] > last:
                continue
            runs.append(ev.normalize(row, name, contaminated=row["label"] in contaminated))
        if unplaced:
            out(f"note: {unplaced} runs of {CURRENT}/results.jsonl are in no plan of the queue log: left out")
        found = sorted({run["round"] for run in runs if ev.round_key(run["round"])[1] >= 5}, key=ev.round_key)
        top = last if last is not None else max([ev.round_key(name)[1] for name in found] or [4])
        rounds += [f"round {number}" for number in range(5, top + 1)]
    return runs, rounds


def summary_lines(changes, pairs, unmatched, no_harness, rounds, written):
    out = [f"# Derived model x harness statuses ({ev.rounds_words(rounds)})", "",
           f"Rules: sandbox/model_evidence.py RULES {json.dumps(ev.RULES)}.", ""]
    counts = {}
    for key, found in pairs.items():
        counts[ev.derive(found)[0]] = counts.get(ev.derive(found)[0], 0) + 1
    out.append("Pairs with data: " + ", ".join(f"{status} {count}" for status, count in sorted(counts.items())))
    out += ["", f"{'Changed' if written else 'Would change'}: {len(changes)} pairs", ""]
    for change in changes:
        family, agent = change["pair"].split("/", 1)
        before = change["before"] or "(no entry)"
        out.append(f"- {family} x {agent}: {before} -> {change['after']} ({change['why']})")
    if unmatched:
        out += ["", "Measured models of no family (no status derived; a profile needs a family first):"]
        out += [f"- {model}: {count} graded runs" for model, count in unmatched.items()]
    if no_harness:
        out += ["", f"Runs straight against the API (no harness): {no_harness}, not a pair."]
    return out


def snapshot_of(pairs, unmatched, no_harness, rounds):
    return {"schema": ev.SCHEMA, "derived": ev.DERIVED, "rules": ev.RULES, "rounds_included": rounds,
            "pairs": pairs, "unmatched_models": unmatched, "no_harness_runs": no_harness}


def dump(path, document):
    Path(path).write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def contradictions(table, snapshot):
    """The pairs where TABLE contradicts SNAPSHOT's numbers, and every status no measured runs back (a family's own
    status; a pair's without its derived evidence block and run ids): ["family x agent: ..."]."""
    found = []
    families = {family["id"]: family for family in table["families"]}
    for key, numbers in snapshot["pairs"].items():
        family_id, agent = key.split("/", 1)
        entry = families.get(family_id, {}).get("harnesses", {}).get(agent)
        status, rule = ev.derive(numbers)
        name = f"{family_id} x {agent}"
        if entry is None:
            found.append(f"{name}: no entry; the numbers give {status}")
        elif entry.get("status") != status:
            found.append(f"{name}: the table says {entry.get('status')}, the numbers give {status} ({rule})")
        elif not isinstance(entry.get("evidence"), dict) or entry["evidence"].get("rule") != rule or \
                any(entry["evidence"].get(k) != v for k, v in numbers.items()):
            found.append(f"{name}: its evidence block is not the snapshot's numbers")
    for family in table["families"]:
        if {"status", "reason"} & set(family.get("all") or {}):
            found.append(f"{family['id']}: a status for every harness, which no pair's runs back (a status is a "
                         "measured pair's; what is known goes into works/fails)")
        for agent, entry in family.get("harnesses", {}).items():
            block = entry.get("evidence")
            key = f"{family['id']}/{agent}"
            if isinstance(block, dict) and block.get("derived") == ev.DERIVED and key not in snapshot["pairs"]:
                found.append(f"{family['id']} x {agent}: derived, but not in the snapshot")
            elif entry.get("status") and not (isinstance(block, dict) and block.get("derived") == ev.DERIVED
                                              and block.get("run_ids")):
                found.append(f"{family['id']} x {agent}: {entry['status']} without measured runs (no evidence block "
                             "with run ids from scripts/profile_status.py)")
            elif entry.get("status") and not (snapshot["pairs"].get(key) or {}).get("run_ids"):
                found.append(f"{family['id']} x {agent}: {entry['status']}, but the snapshot names no runs of it")
    return found


def main(argv=None, out=print):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--research", metavar="DIR", help="the research tree (phase2/, round1-4/, round5/)")
    parser.add_argument("--last-round", type=int, metavar="N", help="read rounds up to N (default: every one)")
    parser.add_argument("--table", default=str(TABLE), help="model_profiles.json (default: the repository's)")
    parser.add_argument("--snapshot", default=str(SNAPSHOT), help="model_evidence.json (default: the repository's)")
    parser.add_argument("--date", action="append", default=[], metavar="ROUND=DATE",
                        help="a round's date for the table's rounds, when it has none (\"round 7=2026-11-20\")")
    parser.add_argument("--write", action="store_true", help="write the table and the snapshot")
    parser.add_argument("--summary", metavar="FILE", help="write what changed and why (Markdown)")
    parser.add_argument("--check", action="store_true", help="compare the table with the snapshot (no research)")
    args = parser.parse_args(argv)
    table = json.loads(Path(args.table).read_text(encoding="utf-8"))
    if args.check:
        found = contradictions(table, json.loads(Path(args.snapshot).read_text(encoding="utf-8")))
        for text in found:
            out(text)
        out(f"{len(found)} contradictions" if found else "the table matches the snapshot")
        return 1 if found else 0
    if not args.research:
        parser.error("--research DIR (or --check)")
    for item in args.date:
        name, _, date = item.partition("=")
        if not ev.ROUND.match(name.strip()) or not date.strip():
            parser.error(f"--date ROUND=DATE: {item!r}")
        table.setdefault("rounds", {}).setdefault(name.strip(), date.strip())
    runs, rounds = read(args.research, args.last_round, out)
    families = mp.check_table(table)["families"]

    def family_of(model):
        family = mp.family_of(model, {"families": families})
        return family["id"] if family else None

    pairs, unmatched, no_harness = ev.aggregate(runs, family_of, table.get("rounds", {}))
    for name in sorted({name for found in pairs.values() for name in found["rounds"]}, key=ev.round_key):
        if name not in table.get("rounds", {}):
            out(f"note: {name} has no date in the table's rounds: give --date \"{name}=DATE\"")
    orphans = [f"{family['id']} x {agent}" for family in table["families"]
               for agent, entry in family.get("harnesses", {}).items()
               if isinstance(entry.get("evidence"), dict) and entry["evidence"].get("derived") == ev.DERIVED and
               f"{family['id']}/{agent}" not in pairs]
    if orphans:
        out(f"{len(orphans)} derived pairs have no data in this run (a lower --last-round, or an incomplete research "
            f"tree): {', '.join(orphans)}")
        if args.write:
            out("nothing written: their status would no longer match the snapshot")
            return 2
    new, changes = ev.apply(table, pairs)
    snapshot = snapshot_of(pairs, unmatched, no_harness, rounds)
    ev.check_private(snapshot, "snapshot")
    for family in new["families"]:
        for agent, entry in family.get("harnesses", {}).items():
            if isinstance(entry.get("evidence"), dict):
                ev.check_private(entry["evidence"], f"{family['id']} x {agent}")
    mp.check_table(new)
    text = summary_lines(changes, pairs, unmatched, no_harness, rounds, args.write)
    for line in text:
        out(line)
    if args.summary:
        Path(args.summary).write_text("\n".join(text) + "\n", encoding="utf-8")
    if args.write:
        dump(args.table, new)
        dump(args.snapshot, snapshot)
    return 0


if __name__ == "__main__":
    sys.exit(main())
