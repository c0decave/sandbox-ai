"""A user's host for end-to-end tests of what users ran into (test_live_regressions.py, the app's too): the real
./sandbox-ai entry point as a subprocess, with only Podman, tmux and npm faked on PATH.

  podman  a per-user image store (GraphRoot under $HOME, as rootless Podman keeps one per user; another user's is
          another HOME), `image exists|inspect` (with --format '{{.Id}} {{index .Labels "sandbox-ai.role"}}' the
          ID and the role of sandbox-ai's own images), `images`, `build` (tags what it is told), `info --format
          {{.Store.GraphRoot}}`, the launcher's host lookup; `pod create` (and any other `run`) is where a start
          stops: its arguments are written to FAKE_PODMAN_STOP_AT and it fails with exit status 99 - the start got
          as far as making its pod. FAKE_PODMAN_POD_HOLD=FILE: `pod create` waits until FILE exists first (a session
          that is still starting). FAKE_PODMAN_EXISTS_STATUS=125: `image exists` fails as Podman does when it cannot
          answer. FAKE_PODMAN_BUILD_FAIL=1: every build fails (=TEXT: each whose arguments have TEXT); FAKE_PODMAN_BUILD_SECONDS: each
          build takes that long. Every call is logged to FAKE_PODMAN_LOG (JSON lines).
          FAKE_PODMAN_RUN=STATUS[:SECONDS]: the start goes on instead - its pod is made (`pod exists|inspect|rm`
          know it), its sandbox (`run --pod`) runs SECONDS and ends with STATUS. FAKE_PODMAN_POD_CREATE_FAIL and
          FAKE_PODMAN_LOOKUP_FAIL=STATUS:MESSAGE: `pod create` or the host lookup says MESSAGE and fails with STATUS.
          FAKE_PODMAN_KEEP=DIR (with FAKE_PODMAN_RUN): the sandbox's session directory is copied to DIR/session (the
          proxy's private directory, /sandbox/proxy, to DIR/proxy).
          FAKE_PODMAN_WIPE_WORK=1 (with FAKE_PODMAN_RUN): the sandbox deletes everything in /work (its bind source),
          as an agent's rm -rf would. FAKE_PODMAN_WRITE_WORK={"RELATIVE": "TEXT"} (with FAKE_PODMAN_RUN): it writes
          those files in /work, as an agent would.
          FAKE_PODMAN_WRITE_HOME={"RELATIVE": "TEXT"} (with FAKE_PODMAN_RUN): it writes those files in its HOME (a
          login made in the sandbox); FAKE_PODMAN_SAYS=FILE: it prints FILE's bytes (what its program says).
          FAKE_PODMAN_POD_RM_FAIL=STATUS:MESSAGE (with FAKE_PODMAN_RUN): `pod rm` says MESSAGE and fails with STATUS
          (a full disk), `pod kill` does nothing, and `pod ps --format json` lists every pod made as running (a
          sandbox that outlives its launcher; FAKE_PODMAN_POD_GONE=1: none, it ended after all). FAKE_PODMAN_POD_LIVE=1:
          `pod ps` so too, while `pod rm` works (a later command, the disk has room again).
          FAKE_PODMAN_POD_PAUSE_FAIL=STATUS:MESSAGE: `pod pause` and `pod unpause` say MESSAGE and fail so.
          FAKE_TOR_SCHEDULE: the Tor container of a Tor profile runs the real vpn_runtime.py with a fake tor whose
          bootstrap goes as the schedule says (FAKE_TOR, below; FAKE_TOR_SPEED, FAKE_TOR_DIR). FAKE_VPN_DIR: a VPN
          container runs the real vpn_runtime.py with the kernel stood in for (FAKE_VPN, below): a start's lent
          tunnel and its MCP servers' borrowing VPN containers meet through a real Unix socket.
          FAKE_PODMAN_STATS=FILE: `podman stats` prints a row for each pod made (its POD-agent container) from
          FILE's JSON object, its counters grown at every call (CPUNano, NetInput, NetOutput, BlockInput, BlockOutput).
          `pull REF` adds REF when it is in the JSON list FAKE_PODMAN_REGISTRY (else it fails); `save REF` writes a
          small archive of it, `load` takes one in (FAKE_PODMAN_LOAD_DIGEST=lost: under another name, as Podman loses
          a registry's digest); `image inspect --format {{.Size}}` says 50 MiB.
          `images search`'s package index (a run named sandbox-ai-package-index-*) prints the file
          FAKE_PODMAN_PACKAGE_INDEX; FAKE_PODMAN_PACKAGE_INDEX_STATUS=3: it fails as without network.
          `volume ls` lists the names of GraphRoot/fake-volumes.json (a JSON list of names, or of {"Name", "Labels"};
          `volume ls --format json`: rows of both), `volume rm NAME` removes one -
          except one named in FAKE_PODMAN_VOLUME_IN_USE (comma-separated): "volume is being used", exit status 2.
          FAKE_PODMAN_IMAGES=FILE: a JSON list of image rows as `podman images --format json` gives them (Id,
          ParentId, Names, Labels, Size, Containers; "UniqueSize" for `system df -v`) answers `images` (--filter
          dangling=true: those without a name nobody builds on), `system df [-v] --format json`, and `image rm REF`
          (an id: the row goes; a name: the name goes, the row with its last one) - never one with Containers, nor one
          named in FAKE_PODMAN_IMAGE_IN_USE (comma-separated): "image is in use by a container", exit status 2.
  tmux    never a real server: every call fails (and is logged).
  ssh     every host is unreachable (exit status 255, "No route to host"); `ssh -G` answers. Logged to FAKE_SSH_LOG.
          Host.route_ssh(TARGET, OTHER): TARGET reaches the other user OTHER instead (its sandbox-ai, with the
          standard input given: an archive streamed to `session import -`).
  npm     `npm view PACKAGE version` answers 2.0.0 (the agents built at their newest release).
  sshfs, fusermount3 (Host.add_sshfs(): not on PATH otherwise): real FUSE is not there. `sshfs TARGET:PATH POINT
          -o ...` logs its arguments (and whether its standard input was a terminal) to FAKE_SSHFS_LOG, writes
          POINT/REMOTE.txt (TARGET:PATH: the remote folder's content) and records POINT in FAKE_FUSE_MOUNTS (a JSON
          list); FAKE_SSHFS_FAIL=STATUS:MESSAGE fails so instead. `fusermount3 -u [-z] POINT` logs too, takes
          REMOTE.txt out again and POINT off the list ("not found in /etc/mtab", exit status 1, when it is not
          there); FAKE_FUSERMOUNT_FAIL=STATUS:MESSAGE fails so (busy).
  sudo, mount.nfs, mount.cifs (Host.add_sudo()): `sudo [-n] mount -t nfs|cifs ...` logs its arguments (and, for
          cifs, the content of its credentials= file) to FAKE_SSHFS_LOG and mounts as the sshfs fake does (the mount
          point: the first argument that is a folder); `sudo [-n] umount [-l] POINT` takes it out again.
          FAKE_SUDO_FAIL=STATUS:MESSAGE fails so ("sudo: a password is required").

These tests may run as uid 0 without capabilities (a sandbox), where the launcher refuses to start ("Run as an
ordinary host user"): python3 on PATH then says euid 1000 (uidshim), and nothing else changes; as an ordinary user
the real python3 runs.
"""
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]

PODMAN = r'''#!PYTHON
import json, os, sys, time
from pathlib import Path
args = sys.argv[1:]
home = Path(os.environ.get("HOME", "/nonexistent"))
root = Path(os.environ.get("FAKE_PODMAN_GRAPHROOT") or home / ".local/share/containers/storage")
log = os.environ.get("FAKE_PODMAN_LOG")
if log:
    with open(log, "a") as stream:
        stream.write(json.dumps(args) + "\n")
while args and args[0].startswith("--"):
    args = args[2:] if args[0] in ("--url", "--connection", "--root", "--runroot") else args[1:]
store = root / "fake-images.json"
pods_file = root / "fake-pods.json"


def pods():
    try:
        return json.loads(pods_file.read_text())
    except (OSError, ValueError):
        return {}


def images():
    try:
        return json.loads(store.read_text())
    except (OSError, ValueError):
        return {}


def norm(name):
    name = name if ":" in name.rsplit("/", 1)[-1] else name + ":latest"
    return name if "/" in name else "localhost/" + name


def option(name):
    return next((args[i + 1] for i, a in enumerate(args) if a == name and i + 1 < len(args)), None)


if os.environ.get("FAKE_PODMAN_IMAGES") and (args[:1] == ["images"] or args[:2] in (
        ["image", "rm"], ["system", "df"], ["image", "ls"])):
    table = Path(os.environ["FAKE_PODMAN_IMAGES"])
    rows = json.loads(table.read_text())
    parents = {row.get("ParentId") for row in rows if row.get("ParentId")}
    if args[:2] == ["image", "rm"]:
        ref = args[-1]
        found = [row for row in rows if row["Id"].startswith(ref) or ref in row.get("Names", [])]
        if not found:
            print(f"Error: {ref}: image not known", file=sys.stderr)
            sys.exit(1)
        row = found[0]
        if row.get("Containers") or ref in os.environ.get("FAKE_PODMAN_IMAGE_IN_USE", "").split(","):
            print(f"Error: image {ref} is in use by a container", file=sys.stderr)
            sys.exit(2)
        if ref in row.get("Names", []) and len(row["Names"]) > 1:
            row["Names"].remove(ref)
        else:
            rows.remove(row)
        table.write_text(json.dumps(rows))
        print(ref)
        sys.exit(0)
    if args[:2] == ["system", "df"]:
        if "-v" in args:
            print(json.dumps({"Images": [{"ImageID": row["Id"][:12], "Size": row.get("Size", 0),
                                          "UniqueSize": row.get("UniqueSize", row.get("Size", 0)),
                                          "Containers": row.get("Containers", 0)} for row in rows],
                              "Containers": [], "Volumes": []}))
        else:
            total = sum(row.get("UniqueSize", row.get("Size", 0)) for row in rows)
            used = sum(row.get("UniqueSize", row.get("Size", 0)) for row in rows if row.get("Containers"))
            active = sum(1 for row in rows if row.get("Containers"))
            print(json.dumps([{"Type": "Images", "Total": len(rows), "Active": active, "RawSize": total,
                               "RawReclaimable": total - used}]))
        sys.exit(0)
    if "dangling=true" in args:
        rows = [row for row in rows if not row.get("Names") and row["Id"] not in parents]
    print(json.dumps([{**row, "Labels": row.get("Labels") or None} for row in rows]))
    sys.exit(0)
if args[:1] == ["info"]:
    print(root)
    sys.exit(0)
if args[:2] == ["image", "exists"]:
    if os.environ.get("FAKE_PODMAN_EXISTS_STATUS"):
        print("Error: creating lock: open /run/user/1000/libpod/tmp/alive.lck: permission denied", file=sys.stderr)
        sys.exit(int(os.environ["FAKE_PODMAN_EXISTS_STATUS"]))
    sys.exit(0 if norm(args[2]) in images() else 1)
if args[:1] == ["build"]:
    time.sleep(float(os.environ.get("FAKE_PODMAN_BUILD_SECONDS") or 0))  # a build that takes a while
    failing_build = os.environ.get("FAKE_PODMAN_BUILD_FAIL")  # 1: every build; else those whose tag has it
    if failing_build and (failing_build == "1" or any(failing_build in arg for arg in args)):
        print("Error: building at STEP \"RUN apt-get update\": exit status 100 (fake)", file=sys.stderr)
        sys.exit(1)
    found, labels, tags, i = images(), {}, [], 1
    while i < len(args):
        if args[i] in ("--tag", "-t", "--label"):
            if args[i] == "--label":
                key, _, value = args[i + 1].partition("=")
                labels[key] = value
            else:
                tags.append(args[i + 1])
            i += 2
            continue
        i += 1
    for tag in tags:
        found[norm(tag)] = {"labels": labels}
    root.mkdir(parents=True, exist_ok=True)
    store.write_text(json.dumps(found))
    print("STEP 1/1: FROM fake")
    sys.exit(0)
if args[:1] == ["images"] or args[:2] in (["image", "ls"], ["image", "list"]):
    if "json" in (option("--format") or "") or "--format=json" in args:
        print(json.dumps([{"Names": [n], "Id": "f" * 64, "Labels": v["labels"], "Size": 1, "Created": 0}
                          for n, v in images().items()]))
    elif option("--format") == "{{.ID}}":  # (every image has the ID f...f, as image inspect says)
        print("\n".join("f" * 64 for _ in images()))
    else:
        print("\n".join(images()))
    sys.exit(0)
if args[:2] == ["image", "inspect"]:
    found = images().get(norm(args[-1]))
    if found is None:
        print(f"Error: {args[-1]}: image not known", file=sys.stderr)
        sys.exit(125)
    shape = option("--format")
    if shape in (None, "json"):
        print(json.dumps([{"Id": "f" * 64, "Labels": found["labels"], "Config": {"Labels": found["labels"]}}]))
    elif shape.startswith("{{.Id}} {{index .Labels "):  # the ID and one label (browser.image_id: sandbox-ai.role)
        print("f" * 64, found["labels"].get(shape.split('"')[1], "<no value>"))
    elif "Labels" in shape:
        print(json.dumps(found["labels"]))
    elif shape == "{{.Size}}":
        print(found.get("size", 52428800))
    else:
        print("")
    sys.exit(0)
if args[:2] in (["pod", "inspect"], ["pod", "exists"]):  # (no pod is ever made: a start stops at pod create)
    if args[1] == "inspect":
        print(f"Error: no such pod {args[-1]}", file=sys.stderr)
    sys.exit(125 if args[1] == "inspect" else 1)
if args[:1] == ["pull"]:
    registry = os.environ.get("FAKE_PODMAN_REGISTRY")  # a JSON list of the references a pull finds
    if registry and args[-1] in json.loads(Path(registry).read_text()):
        found = images()
        found[norm(args[-1])] = {"labels": {}, "pulled": True}
        root.mkdir(parents=True, exist_ok=True)
        store.write_text(json.dumps(found))
        print("Copying blob 0123 done")
        sys.exit(0)
    print(f"Error: the fake podman pulls nothing: {args[-1]}", file=sys.stderr)
    sys.exit(125)
if args[:1] == ["save"]:  # an archive of one image: FAKEIMAGE and its JSON
    found = images().get(norm(args[-1]))
    if found is None:
        print(f"Error: {args[-1]}: image not known", file=sys.stderr)
        sys.exit(125)
    # An OCI archive as podman save writes one: index.json (FAKE_PODMAN_SAVE_NAMES: the names a hostile host's
    # archive carries, a JSON list), and the fake's own record of the image.
    import io, tarfile
    names = json.loads(os.environ.get("FAKE_PODMAN_SAVE_NAMES") or "[null]")
    members = {"oci-layout": {"imageLayoutVersion": "1.0.0"},
               "index.json": {"schemaVersion": 2, "manifests": [
                   {"annotations": {"org.opencontainers.image.ref.name": name} if name else {}} for name in names]},
               "fake-image.json": {"ref": args[-1], **found}}
    with tarfile.open(fileobj=sys.stdout.buffer, mode="w|") as tar:
        for name, value in members.items():
            content = json.dumps(value).encode()
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    sys.exit(0)
if args[:1] == ["load"]:  # FAKE_PODMAN_LOAD_DIGEST=lost: the image arrives without its registry digest
    import tarfile
    source = args[args.index("--input") + 1] if "--input" in args else None
    try:
        with (tarfile.open(source) if source else tarfile.open(fileobj=sys.stdin.buffer, mode="r|")) as tar:
            members = {member.name: json.loads(tar.extractfile(member).read()) for member in tar}
        loaded = members["fake-image.json"]
    except (tarfile.TarError, KeyError, ValueError, OSError):
        print("Error: payload does not match any of the supported image formats", file=sys.stderr)
        sys.exit(125)
    ref = loaded.pop("ref")
    if os.environ.get("FAKE_PODMAN_LOAD_DIGEST") == "lost":
        ref = "localhost/loaded:latest"
    found = images()
    found[norm(ref)] = {**loaded, "loaded": True}
    for manifest in members["index.json"]["manifests"]:  # as Podman does: every name the archive says
        name = (manifest.get("annotations") or {}).get("org.opencontainers.image.ref.name")
        if name:
            found[norm(name)] = {**loaded, "loaded": True}
    root.mkdir(parents=True, exist_ok=True)
    store.write_text(json.dumps(found))
    print(f"Loaded image: {ref}")
    sys.exit(0)
def failing(variable):
    """FAKE_PODMAN_...=STATUS:MESSAGE: MESSAGE on standard error, exit STATUS."""
    given = os.environ.get(variable)
    if given:
        status, _, message = given.partition(":")
        print(message, file=sys.stderr)
        sys.exit(int(status))


if args[:1] == ["run"] and os.environ.get("FAKE_TOR_SCHEDULE") and "localhost/sandbox-ai-tor:latest" in args:
    # The Tor container (FAKE_TOR_SCHEDULE): the real vpn_runtime.py the launcher mounts, its tor a fake (fake_tor).
    sys.path.insert(0, os.environ["FAKE_TOR_FAKES"])
    import fake_tor
    sys.exit(fake_tor.container(args))
if args[:1] == ["run"] and os.environ.get("FAKE_VPN_DIR") and "localhost/sandbox-ai-vpn:latest" in args:
    # A VPN container (FAKE_VPN_DIR): the real vpn_runtime.py the launcher mounts, the kernel a fake (fake_vpn).
    sys.path.insert(0, os.environ["FAKE_TOR_FAKES"])
    import fake_vpn
    sys.exit(fake_vpn.container(args))
if args[:1] == ["run"] and any(a.startswith("--name=sandbox-ai-package-index-") for a in args):
    # images search: the package index read in a throwaway container (FAKE_PODMAN_PACKAGE_INDEX: what it prints,
    # FAKE_PODMAN_PACKAGE_INDEX_STATUS: its exit status - 3 is "no network")
    status = int(os.environ.get("FAKE_PODMAN_PACKAGE_INDEX_STATUS") or 0)
    if status:
        print("W: Failed to fetch http://deb.debian.org/debian/dists/bookworm/InRelease  Temporary failure "
              "resolving 'deb.debian.org'", file=sys.stderr)
        sys.exit(status)
    given = os.environ.get("FAKE_PODMAN_PACKAGE_INDEX")
    print("Get:1 http://deb.debian.org/debian bookworm InRelease", file=sys.stderr)
    sys.stdout.write(Path(given).read_text() if given else "")
    sys.exit(0)
if args[:1] == ["run"] and "--pod" not in args and "-c" in args:
    failing("FAKE_PODMAN_LOOKUP_FAIL")
    names = json.loads(args[args.index("-c") + 2])  # the launcher's host lookup in the image
    print(json.dumps({name: ["192.0.2.1"] for name in names}))
    sys.exit(0)
if os.environ.get("FAKE_PODMAN_RUN") is not None and (args[:2] == ["pod", "create"] or args[:1] == ["run"]):
    # FAKE_PODMAN_RUN=STATUS[:SECONDS]: the pod is made, its sandbox (podman run --pod) runs SECONDS and ends with
    # STATUS (FAKE_PODMAN_POD_CREATE_FAIL=STATUS:MESSAGE: making the pod fails so instead).
    if args[:2] == ["pod", "create"]:
        hold = os.environ.get("FAKE_PODMAN_POD_HOLD")
        for _ in range(1200 if hold else 0):
            if Path(hold).exists():
                break
            time.sleep(0.05)
        failing("FAKE_PODMAN_POD_CREATE_FAIL")
        root.mkdir(parents=True, exist_ok=True)
        made = pods()
        made[option("--name")] = {}
        pods_file.write_text(json.dumps(made))
        print("f" * 64)
        sys.exit(0)
    status, _, seconds = os.environ["FAKE_PODMAN_RUN"].partition(":")
    keep = os.environ.get("FAKE_PODMAN_KEEP")
    if keep:  # FAKE_PODMAN_KEEP=DIR: the session directory the sandbox got (its config.json: the agent's command)
        import shutil
        for arg in args:
            # (and the connection proxy's private directory, /sandbox/proxy: its proxy.json, to DIR/proxy)
            for folder in ("session", "proxy"):
                if arg.startswith("type=bind,") and f",dst=/sandbox/{folder}," in arg:
                    shutil.copytree(arg.split("src=", 1)[1].split(",dst=", 1)[0], Path(keep) / folder,
                                    dirs_exist_ok=True)
    if os.environ.get("FAKE_PODMAN_WIPE_WORK"):  # an agent that deletes everything in /work (its bind source)
        import shutil
        for arg in args:
            if arg.startswith("type=bind,") and ",dst=/work," in arg:
                work = Path(arg.split("src=", 1)[1].split(",dst=", 1)[0])
                for child in work.iterdir():
                    shutil.rmtree(child) if child.is_dir() and not child.is_symlink() else child.unlink()
    if os.environ.get("FAKE_PODMAN_WRITE_WORK"):  # an agent that writes these files ({relative: text}) in /work
        for arg in args:
            if arg.startswith("type=bind,") and ",dst=/work," in arg:
                work = Path(arg.split("src=", 1)[1].split(",dst=", 1)[0])
                for relative, text in json.loads(os.environ["FAKE_PODMAN_WRITE_WORK"]).items():
                    (work / relative).parent.mkdir(parents=True, exist_ok=True)
                    (work / relative).write_text(text)
    if os.environ.get("FAKE_PODMAN_WRITE_HOME"):  # an agent that writes these files ({relative: text}) in its HOME
        for arg in args:
            if arg.startswith("type=bind,") and arg.split("src=", 1)[1].split(",dst=", 1)[0].endswith("/home"):
                home = Path(arg.split("src=", 1)[1].split(",dst=", 1)[0])
                for relative, text in json.loads(os.environ["FAKE_PODMAN_WRITE_HOME"]).items():
                    (home / relative).parent.mkdir(parents=True, exist_ok=True)
                    (home / relative).write_text(text)
    print("fake podman: the sandbox runs", file=sys.stderr)
    if os.environ.get("FAKE_PODMAN_SAYS"):  # what the sandbox's program prints (the file's bytes, as they are)
        sys.stdout.buffer.write(Path(os.environ["FAKE_PODMAN_SAYS"]).read_bytes())
        sys.stdout.flush()
    time.sleep(float(seconds or 0))
    sys.exit(int(status or 0))
if args[:1] == ["stats"] and os.environ.get("FAKE_PODMAN_STATS"):
    # FAKE_PODMAN_STATS=FILE: `podman stats` of every pod made (POD-agent), from FILE's JSON object: each counter
    # grows by its value at every call (CPUNano, NetInput, ...), the others are as they are (MemUsage, MemLimit, PIDs).
    given = Path(os.environ["FAKE_PODMAN_STATS"])
    counter = given.with_name(given.name + ".calls")
    calls = int(counter.read_text()) + 1 if counter.exists() else 1
    counter.write_text(str(calls))
    row = json.loads(given.read_text())
    grown = {key: value * calls if key in ("CPUNano", "NetInput", "NetOutput", "BlockInput", "BlockOutput") else value
             for key, value in row.items()}
    for name in pods():
        print(json.dumps({**grown, "Name": f"{name}-agent"}))
    sys.exit(0)
if args[:2] in (["pod", "exists"], ["pod", "inspect"]):
    if args[-1] not in pods():
        print(f"Error: no such pod {args[-1]}", file=sys.stderr)
        sys.exit(1)
    print("{}")
    sys.exit(0)
if args[:2] == ["pod", "rm"]:
    failing("FAKE_PODMAN_POD_RM_FAIL")
    made = pods()
    if args[-1] in made:
        del made[args[-1]]
        pods_file.write_text(json.dumps(made))
    sys.exit(0)
if args[:2] in (["pod", "stop"], ["pod", "kill"]):
    sys.exit(0)
if args[:2] in (["pod", "pause"], ["pod", "unpause"]):
    failing("FAKE_PODMAN_POD_PAUSE_FAIL")
    sys.exit(0)
if args[:2] in (["volume", "ls"], ["volume", "rm"]):
    volumes_file = root / "fake-volumes.json"
    try:
        volumes = json.loads(volumes_file.read_text())
    except (OSError, ValueError):
        volumes = []
    rows = [{"Name": v, "Labels": {}} if isinstance(v, str) else v for v in volumes]  # (a name, or Name and Labels)
    if args[1] == "ls":
        if "json" in (option("--format") or ""):
            print(json.dumps([{"Name": row["Name"], "Labels": row.get("Labels") or None} for row in rows]))
        else:
            print("\n".join(row["Name"] for row in rows))
        sys.exit(0)
    if args[-1] in os.environ.get("FAKE_PODMAN_VOLUME_IN_USE", "").split(","):
        print(f"Error: volume {args[-1]} is being used by the following container(s): 0123: volume is being used",
              file=sys.stderr)
        sys.exit(2)
    if any(row["Name"] == args[-1] for row in rows):
        volumes = [v for v, row in zip(volumes, rows) if row["Name"] != args[-1]]
        volumes_file.write_text(json.dumps(volumes))
    sys.exit(0)
if args[:2] == ["pod", "ps"]:
    if (os.environ.get("FAKE_PODMAN_POD_RM_FAIL") or os.environ.get("FAKE_PODMAN_POD_LIVE")) and \
            not os.environ.get("FAKE_PODMAN_POD_GONE") and \
            "json" in json.dumps(args):  # what was made runs on (FAKE_PODMAN_POD_GONE: ended after all)
        print(json.dumps([{"Name": name, "Status": "Running", "InfraId": "infra", "Labels": {},
                           "Containers": [{"Id": "infra", "Status": "running"}, {"Id": name, "Status": "running"}]}
                          for name in pods()]))
        sys.exit(0)
    print("[]" if "json" in json.dumps(args) else "")
    sys.exit(0)
if args[:2] == ["pod", "create"] or args[:1] == ["run"]:
    hold = os.environ.get("FAKE_PODMAN_POD_HOLD")
    stop = os.environ.get("FAKE_PODMAN_STOP_AT")
    if stop:  # (whole or not at all: the test reads it while this runs - an empty file was a JSON error under load)
        Path(stop + ".part").write_text(json.dumps(args))
        os.replace(stop + ".part", stop)
    if hold:
        for _ in range(1200):
            if Path(hold).exists():
                break
            time.sleep(0.05)
    print("fake podman: the start stops here: " + " ".join(args[:2]), file=sys.stderr)
    sys.exit(99)
sys.exit(0)
'''

# The Tor container of a Tor profile (podman run ... localhost/sandbox-ai-tor:latest, with FAKE_TOR_SCHEDULE set): the
# real vpn_runtime.py the launcher mounts runs its own main() - the kill switch, the torrc, tor's bootstrap with its
# retries - with only the container's surroundings stood in for: nft keeps its table in a file, tor is this file run
# as `fake_tor.py tor` (each start one attempt: it writes the Bootstrapped lines FAKE_TOR_SCHEDULE gives for it, a JSON
# list per attempt of [seconds, percent, summary], then stays), the data directory and /run/vpn lie in FAKE_TOR_DIR,
# and the checks after the bootstrap answer as a clean Tor exit would. Time runs FAKE_TOR_SPEED times as fast (the
# bootstrap's clock and tor's schedule alike). What happened goes to FAKE_TOR_DIR/events.jsonl: each nft command, each
# tor start (whether tor's state file - its guard - was still there) and each tor that ended.
FAKE_TOR = r'''
import importlib.util, json, os, re, signal, subprocess, sys, time
from pathlib import Path

DIRECTORY = Path(os.environ.get("FAKE_TOR_DIR", "/nonexistent"))
SPEED = float(os.environ.get("FAKE_TOR_SPEED") or 60)


def event(**fields):
    DIRECTORY.mkdir(parents=True, exist_ok=True)
    with open(DIRECTORY / "events.jsonl", "a") as stream:
        stream.write(json.dumps({"at": time.time(), **fields}) + "\n")


def tor():
    attempts = DIRECTORY / "attempts"
    number = int(attempts.read_text()) + 1 if attempts.exists() else 1
    attempts.write_text(str(number))
    state = DIRECTORY / "data" / "state"
    event(tor=number, guard_kept=state.exists())
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(f"Guard of attempt {number}\n")
    signal.signal(signal.SIGTERM, lambda *_: (event(tor_ended=number), sys.exit(0)))
    schedule = json.loads(os.environ["FAKE_TOR_SCHEDULE"])
    lines = schedule[min(number, len(schedule)) - 1]
    print("Nov 14 12:00:00.000 [notice] Tor 0.4.8.13 opening log file.", flush=True)
    begun = time.monotonic()
    for at, percent, summary in lines:
        time.sleep(max(0.0, begun + at / SPEED - time.monotonic()))
        print(f"Nov 14 12:00:00.000 [notice] Bootstrapped {percent}% (fake): {summary}", flush=True)
    while True:
        time.sleep(1)


def container(args):
    # FAKE_TOR_RUNTIME=FILE: that vpn_runtime.py instead of the one the launcher mounts (an older release's).
    source = os.environ.get("FAKE_TOR_RUNTIME") or next(
        re.search(r"src=([^,]+)", a)[1] for a in args if "dst=/vpn/vpn_runtime.py" in a)
    spec = importlib.util.spec_from_file_location("vpn_runtime_in_fake_tor", source)
    rt = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rt)
    rt.RUN, rt.TOR_DATA = DIRECTORY / "run", DIRECTORY / "data"
    rt.TOR_DATA.mkdir(parents=True, exist_ok=True)
    rt.tor_uid = lambda name=None: rt.TOR_UID
    rt.own_data_directory = lambda uid, path=None: None
    if hasattr(rt, "TorDnsFront"):  # (its port 53 needs root; tests/test_tor_dns_ipv4_only.py runs the real one)
        rt.TorDnsFront.start = lambda self: self
    os.chown = lambda path, uid, gid, follow_symlinks=True: None  # (fresh_guard: root's CHOWN in the container)
    table = DIRECTORY / "nft-table"

    class Runner(rt.Runner):
        def run(self, command, data=None, check=True):
            event(nft=command)
            if command[:2] == ["nft", "-f"]:
                table.write_text(data)
            elif command[:2] == ["nft", "list"]:
                if not table.exists():
                    return subprocess.CompletedProcess(command, 1, "", "No such file or directory")
                return subprocess.CompletedProcess(command, 0, table.read_text(), "")
            elif command[:2] in (["nft", "flush"], ["nft", "delete"]):
                table.unlink(missing_ok=True)
            return subprocess.CompletedProcess(command, 0, "", "")

        def spawn(self, command, log):
            assert command[0] == "tor", command
            return super().spawn([sys.executable, __file__, "tor"], log)

    begun = time.monotonic()
    clock = lambda: begun + (time.monotonic() - begun) * SPEED  # noqa: E731
    up_tor = rt.up_tor
    rt.up_tor = lambda payload, runner, out=None: up_tor(payload, runner, out=out, clock=clock,
                                                         sleep=lambda seconds: time.sleep(seconds / SPEED))
    plan = json.loads(os.environ.get("FAKE_TOR_DNS") or "null")
    if plan is None:
        rt.gather_checks = lambda payload, tunnel, runner, **kw: {
            "ipv4": {"ip": "185.220.101.5", "vpn": True, "url": rt.TOR_CHECK},
            "ipv6": {"error": "Operation not permitted", "kind": "blocked"},
            "dns": {"servers": [rt.TOR_DNS], "configured": True, "routes": {}, "answers": {rt.TOR_DNS: True},
                    "egress": ["185.220.101.6"], "onion": {"tor": ["10.192.0.7"], "outside": ["10.192.0.7"]}}}
        rt.resolve_hosts = lambda names, tunnel, tailnet=False, lookup=None: {name: ["192.0.2.1"] for name in names}
    else:
        # FAKE_TOR_DNS={"servfail_for": SECONDS, "failing": [NAME...], "slow": {NAME: SECONDS}, "echo": ANSWER}: the
        # real checks (ECHO: what check.torproject.org answers, by default a Tor exit) and
        # lookups of the names, against a DNSPort that answers SERVFAIL for SECONDS after the first lookup (tor
        # without a circuit yet), always for the names FAILING, and for a SLOW name for its SECONDS after it was
        # first asked; .onion names it maps at once. "silent_for": SECONDS: before that, for SECONDS after the first
        # lookup, it gives no answer at all to any name, .onion ones included (what dns_lookup raises then: "no
        # answer from SERVER" after its timeout). Each lookup goes to events.jsonl.
        first, named = [], {}

        def lookup(name, server, qtype, timeout=4.0, port=53):
            now = clock()
            first or first.append(now)
            named.setdefault(name, now)
            onion = name.endswith(".onion")
            if now - first[0] < plan.get("silent_for", 0):
                time.sleep(timeout / SPEED)
                event(dns=name, server=server, port=port, answer="none")
                raise TimeoutError(f"no answer from {server}")
            failing = not onion and (name in plan.get("failing", []) or now - first[0] < plan.get("servfail_for", 0)
                                     or now - named[name] < (plan.get("slow") or {}).get(name, 0))
            event(dns=name, server=server, port=port, answer="SERVFAIL" if failing else "ok")
            if failing:  # (what dns_answers raises for SERVFAIL; a ValueError before 0.1.95's fix)
                raise rt.DnsError(2) if hasattr(rt, "DnsError") else ValueError("DNS error code 2")
            return ["10.192.0.7"] if onion else ["192.0.2.1"]
        rt.dns_lookup = lookup
        rt.probe_ipv6 = lambda timeout, connect=None: {"error": "Operation not permitted", "kind": "blocked"}
        gather = rt.gather_checks
        echo = json.dumps(plan.get("echo") or {"IsTor": True, "IP": "185.220.101.5"}).encode()  # check.torproject.org
        rt.gather_checks = lambda payload, tunnel, runner, **kw: gather(
            payload, tunnel, runner, get=lambda url, address, timeout: (200, echo), **kw)
    runner = Runner()
    try:
        import inspect
        timed = {"clock": clock, "sleep": lambda seconds: time.sleep(seconds / SPEED)} \
            if "clock" in inspect.signature(rt.main).parameters else {}  # (a vpn_runtime.py before 0.1.95's fix)
        return rt.main([args[-1]], runner=runner, **timed)
    finally:
        for child in runner.children:
            child.terminate()


if __name__ == "__main__" and sys.argv[1:] == ["tor"]:
    tor()
'''

# A VPN container of a WireGuard (or Tailscale, OpenVPN) profile (podman run ... localhost/sandbox-ai-vpn:latest, with
# FAKE_VPN_DIR set): the real vpn_runtime.py the launcher mounts runs its own main() - the kill switch, the tunnel's
# setup, a lent tunnel's lender (the start's container) or its borrower (an MCP server's) - with only the kernel stood in
# for: nft keeps its table in a file, ip and wg answer as on a host, the checks answer as a clean VPN exit would, and a
# borrower's relay binds nothing (its ports need root; the lender is asked all the same). The folder of a lent tunnel
# is the one its --mount names on this host: the lender's Unix socket is real, and so is each borrower's ping to it.
# What happened goes to FAKE_VPN_DIR/events.jsonl: each tunnel brought up (with the names of the secrets it got and
# whether it lends), each tunnel borrowed (from which run), each ping a lender answered. The run's token of a lent
# tunnel: each container that got one says a digest of it and whether it found it in its command line or environment
# (it must come on standard input only), and keeps it in FAKE_VPN_DIR/tokens/CONTAINER for the test to look for in
# logs. FAKE_VPN_LEND_FAIL: the lender cannot start (its socket cannot be bound), with that text.
# FAKE_VPN_LEND_FULL: every connection the lender serves at once is in use (its slots are taken; a look still finds it).
FAKE_VPN = r'''
import hashlib, importlib.util, json, os, re, subprocess, sys
from pathlib import Path

DIRECTORY = Path(os.environ.get("FAKE_VPN_DIR", "/nonexistent"))


def event(**fields):
    DIRECTORY.mkdir(parents=True, exist_ok=True)
    with open(DIRECTORY / "events.jsonl", "a") as stream:
        stream.write(json.dumps(fields) + "\n")


def container(args):
    source = next(re.search(r"src=([^,]+)", a)[1] for a in args if "dst=/vpn/vpn_runtime.py" in a)
    spec = importlib.util.spec_from_file_location("vpn_runtime_in_fake_vpn", source)
    rt = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rt)
    name = args[args.index("--name") + 1]
    rt.RUN = DIRECTORY / f"run-{name}"
    rt.RUN.mkdir(parents=True, exist_ok=True)
    rt.SERVE_STATE = rt.RUN / "serve.json"
    lend = next((a for a in args if ",dst=/run/sandbox-ai-lend" in a), None)
    if lend:
        # The folder on this host; the socket by a relative path in it (a Unix socket's path has at most 107 bytes,
        # and a test's folder is deeper than the container's /run/sandbox-ai-lend).
        rt.LEND_DIR = Path(re.search(r"src=([^,]+)", lend)[1])
        os.chdir(rt.LEND_DIR)
        rt.LEND_SOCKET = Path("lend.sock")
    table = rt.RUN / "nft-table"

    class Runner(rt.Runner):
        def run(self, command, data=None, check=True):
            if command[:2] == ["nft", "-f"]:
                table.write_text((table.read_text() if table.exists() else "") + (data or ""))
            elif command[:2] == ["nft", "list"]:
                return subprocess.CompletedProcess(command, 0, table.read_text() if table.exists() else "", "")
            elif command[:4] == ["ip", "-4", "route", "show"]:
                return subprocess.CompletedProcess(command, 0, "default via 10.0.2.2 dev eth0 proto static\n", "")
            return subprocess.CompletedProcess(command, 0, "", "")

        def spawn(self, command, log):
            raise AssertionError(f"the fake VPN container runs no daemon: {command}")

    def checks(payload, tunnel, runner, **kw):
        servers = list(tunnel.get("dns") or [])
        return {"ipv4": {"ip": "198.51.100.9", "url": "https://am.i.mullvad.net/json"},
                "ipv6": {"error": "Operation not permitted", "kind": "blocked"},
                "dns": {"servers": servers, "configured": bool(servers),
                        "routes": {server: tunnel["interface"] for server in servers},
                        "answers": {server: True for server in servers}, "egress": ["198.51.100.10"]}}
    rt.gather_checks = checks
    rt.resolve_hosts = lambda names, tunnel, tailnet=False, lookup=None: {n: ["192.0.2.1"] for n in names}
    rt.Borrower.start = lambda self: self
    up_wireguard, up_borrowed, facts = rt.up_wireguard, rt.up_borrowed, rt.Lender.facts

    def token_seen(payload):
        token = (payload.get("secrets") or {}).get("lend_token")
        if not isinstance(token, str):
            return
        (DIRECTORY / "tokens").mkdir(parents=True, exist_ok=True)
        (DIRECTORY / "tokens" / name).write_text(token)
        event(container=name, token=hashlib.sha256(token.encode()).hexdigest()[:16],
              token_in_argv=any(token in arg for arg in args), token_in_env=any(token in v for v in os.environ.values()))

    if os.environ.get("FAKE_VPN_LEND_FAIL"):
        def no_lender(self):
            raise OSError(os.environ["FAKE_VPN_LEND_FAIL"])
        rt.Lender.start = no_lender
    if os.environ.get("FAKE_VPN_LEND_FULL"):
        # Every connection the lender serves at once is in use (the start's other servers): its slots are taken.
        import threading
        lender_start = rt.Lender.start

        def full_lender(self):
            self.slots = threading.BoundedSemaphore(1)
            self.slots.acquire()
            event(container=name, lender="full")
            return lender_start(self)
        rt.Lender.start = full_lender

    def wireguard(payload, runner, *more, **kw):
        event(container=name, up="wireguard", secrets=sorted(payload.get("secrets") or {}),
              lend=bool(payload.get("lend")), required=(payload.get("lend") or {}).get("required")
              if isinstance(payload.get("lend"), dict) else None)
        token_seen(payload)
        return up_wireguard(payload, runner, *more, **kw)

    def borrowed(payload, runner, *more, **kw):
        event(container=name, up="borrowed", secrets=sorted(payload.get("secrets") or {}),
              borrow=payload.get("borrow"))
        token_seen(payload)
        tunnel = up_borrowed(payload, runner, *more, **kw)
        event(container=name, borrowed=tunnel.get("lent"))
        return tunnel

    def pinged(self):
        event(container=name, lender="ping", profile=self.profile)
        return facts(self)
    rt.up_wireguard, rt.up_borrowed, rt.Lender.facts = wireguard, borrowed, pinged
    return rt.main([args[-1]], runner=Runner())
'''

TMUX = '''#!/bin/sh
echo "tmux $*" >> "${FAKE_TMUX_LOG:-/dev/null}"
echo "fake tmux: no server" >&2
exit 1
'''

SSH = r'''#!/bin/sh
echo "ssh $*" >> "${FAKE_SSH_LOG:-/dev/null}"
for a in "$@"; do [ "$a" = -G ] && { printf 'hostname ct1\nuser user\nport 22\n'; exit 0; }; done
[ -n "${FAKE_SSH_ROUTES:-}" ] && exec "$FAKE_SSH_PYTHON" "$(dirname "$0")/fake_ssh_route.py" "$@"
echo "ssh: connect to host ct1 port 22: No route to host" >&2
exit 255
'''

# ssh to a host routed by Host.route_ssh(): the remote command runs as that other Host (its HOME, PATH, fakes) with
# this standard input (an archive streamed to `session import -`); a host not routed is unreachable.
SSH_ROUTE = r'''import json, os, subprocess, sys
args = sys.argv[1:]
routes = json.loads(open(os.environ["FAKE_SSH_ROUTES"]).read())
target = args[args.index("--") + 1] if "--" in args else None
if target not in routes or "-O" in args or len(args) <= args.index("--") + 2:
    print(f"ssh: connect to host {target} port 22: No route to host", file=sys.stderr)
    sys.exit(255)
route = routes[target]
done = subprocess.run(["/bin/sh", "-c", args[args.index("--") + 2]], env=route["env"], cwd=route["cwd"])
sys.exit(done.returncode)
'''

NPM = '''#!/bin/sh
[ "$1" = view ] && [ "$3" = version ] && { echo 2.0.0; exit 0; }
echo "fake npm: $*" >&2
exit 1
'''

SSHFS = r'''#!PYTHON
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ.get("FAKE_SSHFS_LOG", os.devnull), "a") as stream:
    stream.write(json.dumps({"tool": "sshfs", "args": args, "tty": os.isatty(0)}) + "\n")
failing = os.environ.get("FAKE_SSHFS_FAIL")
if failing:
    status, _, message = failing.partition(":")
    print(message, file=sys.stderr)
    sys.exit(int(status))
positional = [a for i, a in enumerate(args) if not a.startswith("-") and not (i and args[i - 1] in ("-o", "-p"))]
source, point = positional[0], Path(positional[1])
if not point.is_dir():
    print(f"fuse: bad mount point `{point}': No such file or directory", file=sys.stderr)
    sys.exit(1)
(point / "REMOTE.txt").write_text(source + "\n")
mounts = Path(os.environ["FAKE_FUSE_MOUNTS"])
known = json.loads(mounts.read_text()) if mounts.exists() else []
mounts.write_text(json.dumps(known + [str(point)]))
'''

FUSERMOUNT = r'''#!PYTHON
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ.get("FAKE_SSHFS_LOG", os.devnull), "a") as stream:
    stream.write(json.dumps({"tool": "fusermount3", "args": args}) + "\n")
failing = os.environ.get("FAKE_FUSERMOUNT_FAIL")
if failing and "-z" not in args:
    status, _, message = failing.partition(":")
    print(message, file=sys.stderr)
    sys.exit(int(status))
point = Path(args[-1])
mounts = Path(os.environ["FAKE_FUSE_MOUNTS"])
known = json.loads(mounts.read_text()) if mounts.exists() else []
if str(point) not in known:
    print(f"fusermount3: entry for {point} not found in /etc/mtab", file=sys.stderr)
    sys.exit(1)
(point / "REMOTE.txt").unlink(missing_ok=True)
mounts.write_text(json.dumps([p for p in known if p != str(point)]))
'''

SUDO = r'''#!PYTHON
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
entry = {"tool": "sudo", "args": args}
for arg in args:
    for option in arg.split(","):
        if option.startswith("credentials="):
            entry["credentials"] = Path(option.split("=", 1)[1]).read_text()
with open(os.environ.get("FAKE_SSHFS_LOG", os.devnull), "a") as stream:
    stream.write(json.dumps(entry) + "\n")
failing = os.environ.get("FAKE_SUDO_FAIL")
if failing:
    status, _, message = failing.partition(":")
    print(message, file=sys.stderr)
    sys.exit(int(status))
words = [a for a in args if a != "-n"]
mounts = Path(os.environ["FAKE_FUSE_MOUNTS"])
known = json.loads(mounts.read_text()) if mounts.exists() else []
if words[:1] == ["mount"]:
    point = next(Path(a) for a in words[1:] if a.startswith("/") and Path(a).is_dir())
    source = next(a for a in words[1:] if ":" in a or a.startswith("//"))
    (point / "REMOTE.txt").write_text(source + "\n")
    mounts.write_text(json.dumps(known + [str(point)]))
    sys.exit(0)
if words[:1] == ["umount"]:
    point = Path(words[-1])
    if str(point) not in known:
        print(f"umount: {point}: not mounted.", file=sys.stderr)
        sys.exit(32)
    (point / "REMOTE.txt").unlink(missing_ok=True)
    mounts.write_text(json.dumps([p for p in known if p != str(point)]))
    sys.exit(0)
print("fake sudo: " + " ".join(args), file=sys.stderr)
sys.exit(1)
'''

UIDSHIM = '''"""python3 as an ordinary user (live_fakes.py): os.geteuid() says 1000 instead of 0, nothing else changes."""
import os
import runpy
import sys

os.geteuid = lambda: 1000
sys.executable = os.environ["FAKE_PYTHON"]
args = sys.argv[1:]
while args and args[0].startswith("-") and args[0] not in ("-c", "-m", "-"):
    args = args[1:]
if args[:1] == ["-c"]:
    sys.argv = ["-c", *args[2:]]
    exec(compile(args[1], "<string>", "exec"), {"__name__": "__main__"})
elif args[:1] == ["-m"]:
    sys.argv = [args[1], *args[2:]]
    runpy.run_module(args[1], run_name="__main__", alter_sys=True)
elif args[:1] == ["-"]:  # the script on standard input (install.sh's helpers: python3 -I - ARGS <<'PY')
    sys.argv = args
    exec(compile(sys.stdin.read(), "<stdin>", "exec"), {"__name__": "__main__"})
else:
    sys.argv = args
    runpy.run_path(args[0], run_name="__main__")
'''

# Host.fake_resolver(): python3 with this machine's resolver stood in for (socket.getaddrinfo), the uid as UIDSHIM
# says when FAKE_RESOLVE_EUID is set; every name looked up is appended to FAKE_RESOLVE_LOG.
RESOLVESHIM = '''"""python3 with a fake resolver (live_fakes.py): FAKE_RESOLVE {name: [addresses] | "NONAME" | "NODATA" |
"AGAIN"}; a name it does not list fails as one that does not exist. Every name asked goes to FAKE_RESOLVE_LOG."""
import json
import os
import runpy
import socket
import sys

_TABLE = json.loads(os.environ.get("FAKE_RESOLVE") or "{}")
_ERRORS = {"NONAME": (socket.EAI_NONAME, "Name or service not known"),
           "NODATA": (getattr(socket, "EAI_NODATA", -5), "No address associated with hostname"),
           "AGAIN": (socket.EAI_AGAIN, "Temporary failure in name resolution")}


def _getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    name = host.decode() if isinstance(host, bytes) else str(host)
    with open(os.environ["FAKE_RESOLVE_LOG"], "a") as log:
        log.write(name + "\\n")
    found = _TABLE.get(name, "NONAME")
    if isinstance(found, str):
        raise socket.gaierror(*_ERRORS[found])
    return [(socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "",
             (a, port or 0, 0, 0) if ":" in a else (a, port or 0)) for a in found]


socket.getaddrinfo = _getaddrinfo
if os.environ.get("FAKE_RESOLVE_EUID"):
    os.geteuid = lambda: 1000
''' + UIDSHIM.split("os.geteuid = lambda: 1000\n", 1)[1].replace("import os\nimport runpy\nimport sys\n", "")


# A variant tor of dsh as `sandbox-ai images recipe tor --from dsh` made it before 0.1.95's fixes: in the old layout
# (~/.config/sandbox-ai/images/tor, one agent per name), with the recipe tor that is gone now.
OLD_TOR_DOCUMENT = {"version": 1, "agent": "dsh", "apt": ["torsocks"], "npm": [], "pip": [], "files": []}
OLD_TOR_FRAGMENT = (
    "# torbrowser-launcher is in Debian's contrib section: its archive, signed by Debian's own key.\n"
    "RUN . /etc/os-release \\\n"
    "    && printf 'Types: deb\\nURIs: http://deb.debian.org/debian\\nSuites: %s %s-updates\\nComponents: contrib\\n"
    "Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg\\n' \\\n"
    "       \"$VERSION_CODENAME\" \"$VERSION_CODENAME\" > /etc/apt/sources.list.d/contrib.sources \\\n"
    "    && apt-get update && apt-get install -y --no-install-recommends torbrowser-launcher \\\n"
    "    && rm -rf /var/lib/apt/lists/*\n")




def old_tor_variant(host):
    """The variant tor of dsh from before (OLD_TOR_DOCUMENT) in HOST's configuration; its folder."""
    folder = host.home / ".config/sandbox-ai/images/tor"
    folder.mkdir(mode=0o700, parents=True)
    (folder / "image.json").write_text(json.dumps(OLD_TOR_DOCUMENT, indent=2) + "\n")
    (folder / "Containerfile.fragment").write_text(OLD_TOR_FRAGMENT)
    return folder


# Environment variables of the test process that would change what the launcher does (conftest.py sets some).
DROPPED = ("SANDBOX_AI_NO_AUTO_BUILD", "SANDBOX_AI_MCP_NO_IMAGE_CHECK", "SANDBOX_AI_PROGRESS", "CONTAINER_HOST",
           "CONTAINERS_STORAGE_CONF", "CONTAINERS_CONF", "TMUX", "SUDO_USER", "SANDBOX_AI_APP_TAB")


# Every Host made in this process: conftest.py's guard looks, after each test, at those the test made (guard).
HOSTS = []


def naming(directory, proc=Path("/proc")):
    """{pid: command line} of the processes (this one aside) whose command line names DIRECTORY (an absolute path;
    DIRECTORY/... too, never a folder whose name only starts the same)."""
    text = str(directory)
    if not text.startswith("/") or len(Path(text).parts) < 3:
        raise ValueError(f"not a test's folder: {text!r}")
    text = text.rstrip("/")
    found = {}
    for entry in Path(proc).iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            line = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
        except OSError:
            continue
        at = line.find(text)
        while at >= 0:
            after = line[at + len(text):at + len(text) + 1]
            if after in ("", "/", " ", "'", '"', ":", ","):
                found[int(entry.name)] = line
                break
            at = line.find(text, at + 1)
    return found


def _started(pid, proc=Path("/proc")):
    """The start time of process PID (field 22 of /proc/PID/stat), or None when it is gone."""
    try:
        return (Path(proc) / str(pid) / "stat").read_text().rpartition(")")[2].split()[19]
    except (OSError, IndexError):
        return None


def end_processes(found, grace=3.0):
    """Each process of FOUND ({pid: command line}, as naming gives it) ended by its exact PID: SIGTERM, after GRACE
    seconds SIGKILL - only while it is still the process that was seen (its start time, its command line). The PIDs
    it signalled, sorted."""
    import signal
    import time
    seen = {pid: _started(pid) for pid in found}
    signalled = []

    def same(pid):
        if seen[pid] is None or _started(pid) != seen[pid]:
            return False
        try:
            return (Path("/proc") / str(pid) / "cmdline").read_bytes().replace(b"\0", b" ").decode(
                errors="replace").strip() == found[pid]
        except OSError:
            return False
    for pid in found:
        if same(pid):
            try:
                os.kill(pid, signal.SIGTERM)
                signalled.append(pid)
            except OSError:
                pass
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline and any(same(pid) and not _zombie(pid) for pid in signalled):
        time.sleep(0.05)
    for pid in signalled:
        if same(pid) and not _zombie(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    return sorted(signalled)


def _zombie(pid):
    try:
        return (Path("/proc") / str(pid) / "stat").read_text().rpartition(")")[2].split()[0] == "Z"
    except (OSError, IndexError):
        return True


def outliving(directory, wait=3.0):
    """naming(DIRECTORY) without the processes that end within WAIT seconds (a launcher's child may be ending just
    as the test does) and without zombies."""
    import time
    deadline = time.monotonic() + wait
    while True:
        found = {pid: line for pid, line in naming(directory).items() if not _zombie(pid)}
        if not found or time.monotonic() >= deadline:
            return found
        time.sleep(0.1)


def guard(hosts, wait=3.0):
    """What the HOSTS (made by one test) left running - a line for each process, ended now by its exact PID; [] when
    nothing. conftest.py fails the test with these lines: it stops what it started (an MCP server outlives its start:
    `sandbox-ai mcp stop NAME`, Host.leftovers)."""
    problems = []
    for host in hosts:
        found = outliving(host.directory, wait=wait)
        if found:
            end_processes(found)
            problems += [f"PID {pid}: {line[:400]}" for pid, line in sorted(found.items())]
    return problems


def guard_since(before, wait=3.0):
    """guard() of the Hosts made since there were BEFORE of them (one test's), which then are no longer kept."""
    made = HOSTS[before:]
    del HOSTS[before:]
    return guard(made, wait=wait)


class Host:
    """A user (HOME, XDG folders, their own Podman image store) with the fakes on PATH, below DIRECTORY."""

    def __init__(self, directory, user="user"):
        HOSTS.append(self)
        self.directory = Path(directory)
        self.bin = self.directory / "fake-bin"
        self.home = self.directory / user
        self.workspace = self.directory / "ws"
        self.runtime = self.directory / f"xdg-{user}"
        for path in (self.bin, self.home, self.workspace):
            path.mkdir(parents=True, exist_ok=True)
        self.runtime.mkdir(mode=0o700, exist_ok=True)
        self.log_path, self.stop_path = self.directory / "podman.log", self.directory / "stopped.json"
        for name, text in (("podman", PODMAN.replace("PYTHON", sys.executable, 1)), ("tmux", TMUX), ("npm", NPM),
                           ("ssh", SSH)):
            (self.bin / name).write_text(text)
            (self.bin / name).chmod(0o755)
        (self.bin / "fake_tor.py").write_text(FAKE_TOR)
        (self.bin / "fake_vpn.py").write_text(FAKE_VPN)
        self.shimmed = os.geteuid() == 0
        if self.shimmed:
            (self.bin / "uidshim.py").write_text(UIDSHIM)
            (self.bin / "python3").write_text(f'#!/bin/sh\nexec {sys.executable} -I "{self.bin}/uidshim.py" "$@"\n')
            (self.bin / "python3").chmod(0o755)
        self.env = {key: value for key, value in os.environ.items() if key not in DROPPED}
        self.env.update(HOME=str(self.home), XDG_RUNTIME_DIR=str(self.runtime),
                        XDG_CONFIG_HOME=str(self.home / ".config"), XDG_DATA_HOME=str(self.home / ".local/share"),
                        XDG_STATE_HOME=str(self.home / ".local/state"), XDG_CACHE_HOME=str(self.home / ".cache"),
                        PATH=f"{self.bin}:/usr/bin:/bin", FAKE_PODMAN_LOG=str(self.log_path),
                        FAKE_PODMAN_STOP_AT=str(self.stop_path), FAKE_TMUX_LOG=str(self.directory / "tmux.log"),
                        FAKE_SSH_LOG=str(self.directory / "ssh.log"), FAKE_TOR_FAKES=str(self.bin),
                        FAKE_TOR_DIR=str(self.directory / "tor-container"),
                        SANDBOX_AI_BUILD_NO_DNS_CHECK="1", LC_ALL="C.UTF-8", TERM="dumb",
                        # (the fake Podman has its init binary, whatever this machine has; =none: a host without)
                        SANDBOX_AI_INIT_BINARY="/usr/libexec/podman/catatonit")
        if self.shimmed:
            self.env["FAKE_PYTHON"] = str(self.bin / "python3")
        else:
            self.env.pop("FAKE_PYTHON", None)

    @property
    def store(self):
        """This user's Podman image store (GraphRoot), as the fake keeps it."""
        return self.home / ".local/share/containers/storage"

    def images(self, store=None):
        """{image: labels} in STORE (default: this user's)."""
        try:
            return json.loads(((store or self.store) / "fake-images.json").read_text())
        except OSError:
            return {}

    def add_image(self, *names, store=None, labels=None):
        """NAMES made present in STORE (default: this user's) without a build, with LABELS."""
        store = store or self.store
        found = self.images(store)
        found.update({name: {"labels": dict(labels or {})} for name in names})
        store.mkdir(parents=True, exist_ok=True)
        (store / "fake-images.json").write_text(json.dumps(found))

    def calls(self):
        """Every podman call so far ([args])."""
        if not self.log_path.exists():
            return []
        # (whole lines only: the fake may be writing the last one just now)
        return [json.loads(line) for line in self.log_path.read_text().rpartition("\n")[0].splitlines()]

    def builds(self):
        return [call for call in self.calls() if "build" in call[:3]]

    def stopped_at(self):
        """The arguments of the `pod create` (or `run`) the start stopped at, else None."""
        return json.loads(self.stop_path.read_text()) if self.stop_path.exists() else None

    def add_sshfs(self):
        """sshfs and fusermount3 fakes on PATH (see above); their calls: sshfs_calls(), what is mounted: fuse_mounts()."""
        for name, text in (("sshfs", SSHFS), ("fusermount3", FUSERMOUNT)):
            (self.bin / name).write_text(text.replace("PYTHON", sys.executable, 1))
            (self.bin / name).chmod(0o755)
        self.env.update(FAKE_SSHFS_LOG=str(self.directory / "sshfs.log"),
                        FAKE_FUSE_MOUNTS=str(self.directory / "fuse-mounts.json"))

    def add_sudo(self):
        """sudo (with mount and umount) and mount.nfs, mount.cifs fakes on PATH (see above), with add_sshfs's log."""
        if "FAKE_FUSE_MOUNTS" not in self.env:
            self.add_sshfs()
        (self.bin / "sudo").write_text(SUDO.replace("PYTHON", sys.executable, 1))
        (self.bin / "sudo").chmod(0o755)
        for name in ("mount.nfs", "mount.cifs"):
            (self.bin / name).write_text("#!/bin/sh\nexit 1\n")
            (self.bin / name).chmod(0o755)

    def sshfs_calls(self):
        path = self.directory / "sshfs.log"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def fuse_mounts(self):
        path = self.directory / "fuse-mounts.json"
        return json.loads(path.read_text()) if path.exists() else []

    def leftovers(self, wait=3.0):
        """{pid: command line} of what still runs of this user's (every process naming its folder), after WAIT
        seconds for those ending just now: {} once a test stopped what it started."""
        return outliving(self.directory, wait=wait)

    def fake_resolver(self, table):
        """python3 on PATH with this machine's resolver stood in for (RESOLVESHIM): TABLE {name: [addresses] |
        "NONAME" | "NODATA" | "AGAIN"}; every other name does not exist. Returns the file of the names looked up."""
        log = self.directory / "resolved.log"
        log.write_text("")
        (self.bin / "resolveshim.py").write_text(RESOLVESHIM)
        (self.bin / "python3").write_text(f'#!/bin/sh\nexec {sys.executable} -I "{self.bin}/resolveshim.py" "$@"\n')
        (self.bin / "python3").chmod(0o755)
        self.env.update(FAKE_RESOLVE=json.dumps(table), FAKE_RESOLVE_LOG=str(log), FAKE_PYTHON=str(self.bin / "python3"))
        if self.shimmed:
            self.env["FAKE_RESOLVE_EUID"] = "1"
        return log

    def route_ssh(self, target, other):
        """ssh from this user to TARGET (USER@HOST as `sandbox-ai hosts add` was given it) reaches OTHER (a Host): its
        sandbox-ai (this checkout, as ~/.local/bin/sandbox-ai there, where a non-interactive login finds it) runs as
        that user. SSH multiplexing is off (no shared connection to fake)."""
        routes_file = self.directory / "ssh-routes.json"
        routes = json.loads(routes_file.read_text()) if routes_file.exists() else {}
        (other.home / ".local/bin").mkdir(parents=True, exist_ok=True)
        link = other.home / ".local/bin/sandbox-ai"
        if not link.exists():
            link.symlink_to(ROOT / "sandbox-ai")
        routes[target] = {"env": other.env, "cwd": str(other.workspace)}
        routes_file.write_text(json.dumps(routes))
        (self.bin / "fake_ssh_route.py").write_text(SSH_ROUTE)
        self.env.update(FAKE_SSH_ROUTES=str(routes_file), FAKE_SSH_PYTHON=sys.executable,
                        SANDBOX_AI_SSH_MULTIPLEX="0")

    def command(self, *args):
        return [str(ROOT / "sandbox-ai"), *map(str, args)]

    def run(self, *args, timeout=300, env=None, cwd=None, **kwargs):
        """`sandbox-ai ARGS` as this user ran it in a script (no terminal): CompletedProcess, text."""
        return subprocess.run(self.command(*args), cwd=cwd or self.workspace, env={**self.env, **(env or {})},
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout, **kwargs)

    def popen(self, *args, env=None, **kwargs):
        return subprocess.Popen(self.command(*args), cwd=self.workspace, env={**self.env, **(env or {})},
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                **kwargs)
