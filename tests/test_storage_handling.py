"""Handling what the kept HOMEs hold (sandbox-ai gc): build output, toolchains and old transcripts removed by kind,
and a stopped kept HOME packed into one archive and unpacked again before it is used - always shown first and asked
(or --yes), never while the start runs, never through a link, nothing lost on a failure. End to end through
./sandbox-ai on a fake host (tests/live_fakes.py), and the functions themselves."""
import importlib.util
import json
import os
from pathlib import Path
import time

import pytest

from live_fakes import Host, ROOT

SPEC = importlib.util.spec_from_file_location("sandbox_statedirs_handling_test", ROOT / "sandbox/statedirs.py")
sd = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sd)


@pytest.fixture
def host(tmp_path):
    h = Host(tmp_path)
    h.env.pop("SANDBOX_AI_RUN", None)  # (this suite may run inside a sandbox-ai run)
    return h


def write(path, size=0, text=None, age_days=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text) if text is not None else path.write_bytes(b"x" * size)
    if age_days is not None:
        when = time.time() - age_days * 86400
        os.utime(path, (when, when))
    return path


def fill(home):
    """A HOME as an agent leaves it: caches, a toolchain, transcripts old and new, build output, its own work."""
    write(home / ".cache/pip/wheel", 30_000)
    write(home / ".rustup/toolchains/stable/rustc", 40_000)
    write(home / "go/src/mine/main.go", text="package main\n")  # the agent's sources in GOPATH: never a toolchain
    write(home / ".claude/projects/p/old-1.jsonl", 20_000, age_days=60)
    write(home / ".claude/projects/p/old-2.jsonl", 20_000, age_days=50)
    write(home / ".claude/projects/p/new.jsonl", 20_000, age_days=1)
    write(home / ".claude/projects/q/only.jsonl", 20_000, age_days=90)  # the newest of its folder: kept
    write(home / ".codex/sessions/2026/09/01/r.jsonl", 10_000, age_days=70)
    write(home / ".codex/sessions/2026/11/15/s.jsonl", 10_000, age_days=1)
    write(home / "work/app/node_modules/x/index.js", 50_000)
    write(home / "work/rs/target/CACHEDIR.TAG", text="Signature: 8a477f597d28d172789f06886806bc55")
    write(home / "work/rs/target/debug/bin", 60_000)
    write(home / "work/app/src/main.js", text="console.log(1)\n")
    write(home / "notes.md", text="mine\n")
    (home / "work/app/node_modules/x").chmod(0o555)  # read-only, as some packages leave it
    return home


def kept_session(host, name, last_used_days=40):
    """A stopped kept start (its run ended) whose HOME the agent filled."""
    directory = host.home / ".local/state/sandbox-ai/sessions" / name
    (directory / "tmux").mkdir(parents=True, mode=0o700)
    when = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - last_used_days * 86400))
    data = {"agent": "claude", "options": [], "agent_args": [], "backend": "podman", "podman_url": None,
            "workspace": None, "cwd": str(host.workspace), "created": when, "cwd_project": None, "ready": True,
            "name": name, "project": host.workspace.name, "keep": True, "mode": "tmux",
            "pod": "agent-claude-0123abcd", "pid": 2 ** 22 + 17, "pid_start": 1, "started": when}
    (directory / "session.json").write_text(json.dumps(data))
    (directory / "exit.json").write_text(json.dumps({"status": 0, "ended": when}))
    return fill(directory / "home")


def files(home):
    return sorted(str(path.relative_to(home)) for path in home.rglob("*") if path.is_file() or path.is_symlink())


# -- removal by kind -----------------------------------------------------------------------------------------------

def test_home_removable_and_removal_by_kind(tmp_path):
    home = fill(tmp_path / "home")
    outside = write(tmp_path / "outside/old.jsonl", 5000, age_days=400)
    write(tmp_path / "outside/newer.jsonl", 5000, age_days=300)
    (home / ".codex/archived_sessions").symlink_to(outside.parent)  # a transcript folder that is a link: never
    found = sd.home_removable(sd.Scan(), home, ["build", "toolchains", "transcripts"], days=30)
    paths = {item["path"]: item for item in found}
    assert set(paths) == {"work/app/node_modules", "work/rs/target", ".rustup", ".claude/projects", ".codex/sessions"}
    assert paths[".claude/projects"]["files"] == 2 and paths[".codex/sessions"]["files"] == 1
    assert sd.home_removable(sd.Scan(), home, ["build"]) == [item for item in found if item["kind"] == "build"]
    assert sd.home_removable(sd.Scan(), home, ["transcripts"]) == []  # no DAYS: no transcript
    before = files(home)
    done = sd.remove_home_items(home, found, days=30)
    assert {item["path"] for item in done} == set(paths)
    left = files(home)
    assert not (home / "work/app/node_modules").exists() and not (home / "work/rs/target").exists()
    assert not (home / ".rustup").exists() and (home / "go/src/mine/main.go").exists()
    assert ".claude/projects/p/new.jsonl" in left and ".claude/projects/q/only.jsonl" in left
    assert ".claude/projects/p/old-1.jsonl" not in left and ".codex/sessions/2026/11/15/s.jsonl" in left
    assert not (home / ".codex/sessions/2026/09").exists()  # emptied folders go too
    assert {".cache/pip/wheel", "notes.md", "work/app/src/main.js"} <= set(left) and len(before) > len(left)
    assert outside.exists()  # never through a link


def test_removal_checks_again_and_never_goes_through_a_link(tmp_path):
    home = fill(tmp_path / "home")
    found = sd.home_removable(sd.Scan(), home, ["build", "transcripts"], days=30)
    # meanwhile: a folder replaced by a link to elsewhere, a transcript written again
    elsewhere = tmp_path / "elsewhere"
    write(elsewhere / "keep", 10)
    sd.remove_tree(home / "work/rs/target")
    (home / "work/rs/target").symlink_to(elsewhere)
    os.utime(home / ".claude/projects/p/old-1.jsonl")
    done = sd.remove_home_items(home, found, days=30)
    assert (elsewhere / "keep").exists() and "work/rs/target" not in {item["path"] for item in done}
    assert (home / ".claude/projects/p/old-1.jsonl").exists()
    assert sd.remove_home_items(tmp_path / "missing", found, days=30) == []
    assert sd.home_removable(sd.Scan(), tmp_path / "missing", ["build", "toolchains", "transcripts"], days=1) == []


def test_gc_removes_by_kind_only_after_showing_and_asking_end_to_end(host):
    home = kept_session(host, "big")
    before = files(home)
    # no terminal, no --yes: shown, nothing removed
    refused = host.run("gc", "--build-output", "--no-compact")
    assert refused.returncode == 1, refused.stdout + refused.stderr
    assert "would remove in the stopped kept HOMEs" in refused.stdout and "work/app/node_modules" in refused.stdout
    assert "--yes" in refused.stderr and files(home) == before
    dry = host.run("gc", "--build-output", "--toolchains", "--transcripts", "30", "--dry-run", "--json",
                   "--no-compact")
    assert dry.returncode == 0, dry.stderr
    planned = {item["path"] for item in json.loads(dry.stdout)["home_items"]}
    assert planned == {"work/app/node_modules", "work/rs/target", ".rustup", ".claude/projects", ".codex/sessions"}
    assert files(home) == before
    done = host.run("gc", "--build-output", "--transcripts", "30", "--yes", "--no-compact")
    assert done.returncode == 0, done.stderr
    assert "removed in the stopped kept HOMEs" in done.stdout and "Claude transcripts" in done.stdout
    assert not (home / "work/app/node_modules").exists() and (home / ".rustup").exists()
    assert (home / ".claude/projects/p/new.jsonl").exists() and (home / "work/app/src/main.js").exists()
    # --home: only that start; one that is not there is refused
    other = kept_session(host, "other")
    assert host.run("gc", "--toolchains", "--home", "nope", "--yes").returncode == 1
    only = host.run("gc", "--toolchains", "--home", "big", "--yes", "--no-compact")
    assert only.returncode == 0, only.stderr
    assert not (home / ".rustup").exists() and (other / ".rustup").exists()
    assert host.run("gc", "--home", "big").returncode == 1  # --home goes with what it narrows
    assert host.run("gc", "--yes").returncode == 1


def test_gc_by_kind_leaves_a_start_another_command_holds(host):
    import fcntl
    home = kept_session(host, "held")
    with open(home.parent / ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # being started or stopped by another command
        result = host.run("gc", "--build-output", "--yes", "--no-compact")
    assert result.returncode == 0, result.stderr
    assert "left alone: held" in result.stdout and (home / "work/app/node_modules").exists()


# -- pack and unpack -----------------------------------------------------------------------------------------------

def test_pack_then_resume_unpacks_it_end_to_end(host):
    host.add_image("localhost/claude-sandbox:latest")
    home = kept_session(host, "old", last_used_days=40)
    recent = kept_session(host, "recent", last_used_days=2)
    before = files(home)
    refused = host.run("gc", "--pack", "--older-than", "30")
    assert refused.returncode == 1 and "would pack" not in refused.stdout and "old" in refused.stdout
    assert "--yes" in refused.stderr and home.is_dir()
    dry = host.run("gc", "--pack", "--older-than", "30", "--dry-run")
    assert dry.returncode == 0 and "would pack: 1 HOME(s)" in dry.stdout and "recent" not in dry.stdout
    assert home.is_dir()
    packed = host.run("gc", "--pack", "--older-than", "30", "--yes", "--json")
    assert packed.returncode == 0, packed.stderr
    result = json.loads(packed.stdout)
    assert [item["name"] for item in result["packed"]] == ["old"] and result["skipped"] == []
    assert not home.exists() and recent.is_dir()
    record = json.loads((home.parent / "home.pack.json").read_text())
    assert (home.parent / record["archive"]).stat().st_size == result["packed"][0]["archive_bytes"]
    usage = json.loads(host.run("gc", "--usage", "--json").stdout)
    item = next(item for item in usage["parts"]["starts"]["items"] if item["name"] == "old")
    assert item["packed"]["archive"] == record["archive"] and item["home"]["bytes"] == 0
    assert "stopped, kept, packed" in host.run("gc", "--usage").stdout
    # gc by kind leaves a packed HOME alone (said); --pack again: nothing to pack
    assert "its HOME is packed" in host.run("gc", "--build-output", "--yes", "--no-compact").stdout
    assert "nothing to pack" in host.run("gc", "--pack", "--older-than", "30", "--yes").stdout
    # --review-home unpacks it first (under the start's lock), then resume uses it as it was
    review = host.run("resume", "old", "--review-home")
    assert review.returncode == 0, review.stderr
    assert "unpacking the HOME of old" in review.stderr and files(home) == before
    host.run("gc", "--pack", "--home", "old", "--yes")
    assert not home.exists()
    resumed = host.run("resume", "old")
    assert "unpacking the HOME of old" in resumed.stderr, resumed.stderr
    assert host.stopped_at() is not None, "the resume got to its pod: " + resumed.stderr
    # (the start went on: it put its canaries into the HOME, as every start does)
    assert set(before) <= set(files(home)) and not (home.parent / "home.pack.json").exists()
    assert (home / "notes.md").read_text() == "mine\n"


def test_unpack_by_hand_and_what_is_refused(host):
    home = kept_session(host, "old")
    before = files(home)
    assert host.run("gc", "--unpack", "old").returncode == 1  # not packed
    assert host.run("gc", "--pack", "--home", "old", "--yes").returncode == 0 and not home.exists()
    assert host.run("gc", "--pack", "--caches").returncode == 1  # --pack does only that
    assert host.run("gc", "--usage", "--pack").returncode == 1
    assert host.run("gc", "--unpack", "old", "--dry-run").returncode == 1
    done = host.run("gc", "--unpack", "old")
    assert done.returncode == 0 and "unpacked the HOME of old" in done.stdout and files(home) == before


def test_a_damaged_archive_is_never_unpacked_over_and_stays(host):
    host.add_image("localhost/claude-sandbox:latest")
    home = kept_session(host, "old")
    host.run("gc", "--pack", "--home", "old", "--yes")
    record = json.loads((home.parent / "home.pack.json").read_text())
    archive = home.parent / record["archive"]
    good = archive.read_bytes()
    archive.write_bytes(good[:len(good) // 2])
    resumed = host.run("resume", "old")
    assert resumed.returncode != 0 and "could not be unpacked" in resumed.stderr, resumed.stderr
    assert host.stopped_at() is None, "never started with an empty HOME"
    assert not home.exists() and archive.exists()  # nothing lost: the archive stays as it was
    archive.write_bytes(good)
    assert host.run("gc", "--unpack", "old").returncode == 0 and (home / "notes.md").read_text() == "mine\n"


def test_a_running_start_is_never_packed_nor_cleared(host):
    """A start whose pod still runs (orphaned: its supervisor gone) is no candidate for --pack or removal by kind;
    named with --home, --pack refuses it."""
    home = kept_session(host, "busy")
    (home.parent / "exit.json").unlink()  # its run did not end
    host.store.mkdir(parents=True, exist_ok=True)
    (host.store / "fake-pods.json").write_text(json.dumps({"agent-claude-0123abcd": {}}))
    live = {"FAKE_PODMAN_POD_LIVE": "1"}
    refused = host.run("gc", "--pack", "--home", "busy", "--yes", env=live)
    assert refused.returncode == 1 and "busy is orphaned" in refused.stderr, refused.stdout + refused.stderr
    assert "nothing to pack" in host.run("gc", "--pack", "--yes", env=live).stdout
    cleared = host.run("gc", "--build-output", "--yes", "--no-compact", env=live)
    assert cleared.returncode == 0 and home.is_dir() and (home / "work/app/node_modules").exists()


# -- Podman: why its "reclaimable" is far more than gc --images frees, and the levels of gc --images -------------------

def image_table(host):
    """A user's Podman images: sandbox-ai's (used by a kept start, unused, unnamed, a role), other software's (a base
    image, a dangling one), a layer an image is built on, one a container uses."""
    ident = lambda c: c * 64  # noqa: E731
    ours = {"sandbox-ai.agent-version": "2.0.0"}
    rows = [
        {"Id": ident("a"), "ParentId": ident("e"), "Names": ["localhost/claude-sandbox:latest"], "Labels": ours,
         "Size": 3 << 30, "UniqueSize": 1 << 30, "Containers": 0},
        {"Id": ident("b"), "Names": ["localhost/codex-sandbox:latest", "localhost/codex-sandbox:base"],
         "Labels": ours, "Size": 4 << 30, "UniqueSize": 2 << 30, "Containers": 0},
        {"Id": ident("c"), "Names": [], "Labels": ours, "Size": 3 << 30, "UniqueSize": 512 << 20, "Containers": 0},
        {"Id": ident("d"), "Names": [], "Labels": {}, "Size": 700 << 20, "UniqueSize": 700 << 20, "Containers": 0},
        {"Id": ident("e"), "Names": [], "Labels": {}, "Size": 2 << 30, "UniqueSize": 0, "Containers": 0},
        {"Id": ident("f"), "Names": ["docker.io/library/debian:bookworm"], "Labels": {}, "Size": 120 << 20,
         "UniqueSize": 120 << 20, "Containers": 0},
        {"Id": "1" * 64, "Names": ["localhost/sandbox-ai-browser:latest"], "Labels": {"sandbox-ai.role": "browser"},
         "Size": 900 << 20, "UniqueSize": 900 << 20, "Containers": 0},
        {"Id": "2" * 64, "Names": ["localhost/goose-sandbox:latest"], "Labels": ours, "Size": 1 << 30,
         "UniqueSize": 1 << 30, "Containers": 1}]
    table = host.directory / "images.json"
    table.write_text(json.dumps(rows))
    host.env["FAKE_PODMAN_IMAGES"] = str(table)
    return table


def names(table):
    return sorted(name for row in json.loads(table.read_text()) for name in row["Names"] or [row["Id"][:1]])


def test_gc_usage_says_what_podmans_images_are(host):
    image_table(host)
    kept_session(host, "mine")  # a kept start of claude: its image is used
    usage = json.loads(host.run("gc", "--usage", "--json").stdout)
    groups = usage["podman"]["groups"]["groups"]
    assert {key: value["count"] for key, value in groups.items() if value["count"]} == {
        "sandbox_used": 1, "sandbox_unused": 1, "sandbox_unnamed": 1, "other_unnamed": 1, "intermediate": 1,
        "other": 1, "sandbox_roles": 1, "in_use": 1}
    assert groups["sandbox_unused"]["bytes"] == 2 << 30  # what removing it frees, not its size
    levels = usage["would_free"]["images_levels"]
    assert levels["unnamed"]["bytes"] == 512 << 20 and levels["old"]["bytes"] == (2 << 30) + (512 << 20)
    assert levels["prune"]["count"] == 3
    assert usage["would_free"]["images"] == {"count": 1, "bytes": 512 << 20}  # what removing it frees
    text = host.run("gc", "--usage").stdout
    said = " ".join(text.split())  # (the tables wrap their last column)
    assert "Podman images by what they are to sandbox-ai" in text
    assert "sandbox-ai gc --images=old" in said and "podman image prune -a" in said
    assert "--images=old frees 2.0 GiB more" in said


def test_gc_images_levels_list_ask_and_remove_only_their_own(host):
    table = image_table(host)
    kept_session(host, "mine")
    before = names(table)
    refused = host.run("gc", "--images=old", "--no-compact")
    assert refused.returncode == 1 and "localhost/codex-sandbox:latest" in refused.stdout and "--yes" in refused.stderr
    assert names(table) == before
    dry = host.run("gc", "--images", "old", "--dry-run", "--json", "--no-compact")
    plan = json.loads(dry.stdout)["image_plan"]
    assert {image["group"] for image in plan} == {"sandbox_unused", "sandbox_unnamed"} and names(table) == before
    done = host.run("gc", "--images=old", "--yes", "--no-compact")
    assert done.returncode == 0, done.stderr
    left = names(table)
    assert "localhost/codex-sandbox:latest" not in left and "localhost/codex-sandbox:base" not in left
    assert "c" not in left  # sandbox-ai's unnamed one
    assert {"localhost/claude-sandbox:latest", "docker.io/library/debian:bookworm", "d", "e",
            "localhost/sandbox-ai-browser:latest", "localhost/goose-sandbox:latest"} <= set(left)
    # prune: also other software's unnamed ones - one in use stays, said
    pruned = host.run("gc", "--images=prune", "--yes", "--no-compact", env={"FAKE_PODMAN_IMAGE_IN_USE": "d" * 12})
    assert pruned.returncode == 0 and "left alone" in pruned.stdout and "d" in names(table)
    assert host.run("gc", "--images=prune", "--yes", "--no-compact").returncode == 0
    assert "d" not in names(table) and "docker.io/library/debian:bookworm" in names(table)
    assert "e" in names(table)  # a layer something is built on: never on its own
    # plain --images: as before, sandbox-ai's unnamed ones only, unasked
    assert host.run("gc", "--images", "--no-compact").returncode == 0
    assert host.run("gc", "--images=everything").returncode == 2
