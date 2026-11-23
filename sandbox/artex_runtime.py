#!/usr/bin/env python3
"""The runtime of sandbox-ai's ARTEX container (Containerfile.artex; --artex, artex.py): mounted read-only at
/sandbox-artex at every start with its configuration, run by the image's python3 as the image's user artex (1000), so
that a new sandbox-ai needs no rebuild of the image.

  artex_runtime.py serve     (the container's command) PostgreSQL, then ARTEX, until the run ends
  artex_runtime.py connect   one connection of the console's forward: this process's standard input and output to
                             ARTEX's console on the container's loopback (artex.py runs it with podman exec for each
                             connection to the host's 127.0.0.1:PORT)

serve, in this order:
- the data folder (DATA: a tmpfs, or the session's Podman volume with --artex-data session): pg (PostgreSQL's cluster),
  data (ARTEX's -data: its SQLite stores, its workspace), keys (the JWT signing key, outside the workspace ARTEX's file
  manager shows), skills (ARTEX's skills, copied from the image the first time), home;
- PostgreSQL 15 of the image, on a Unix socket only (listen_addresses empty: no TCP port in the pod, the agent
  reaches none of it), trusting only this user;
- ARTEX on 127.0.0.1:PORT (its console and API) and 127.0.0.1:PROXY_PORT (its recording proxy), with sandbox-ai's
  build's -admin-password-file: the password sandbox-ai chose reaches ARTEX through a pipe that only ARTEX reads,
  never its environment, and is set before ARTEX listens; the console is ready (the port the sandbox waits for)
  only then. ARTEX's environment is made here, not inherited: its LLM's placeholders (never a key: the connection
  proxy adds it on the wire), the run's CA, the switches that keep its tools from installing anything, and nothing
  else of the container's.

SIGTERM (the run ends) stops ARTEX, then PostgreSQL (its fast shutdown). Either ending ends the container.

Standard library only (Python 3.9 or later; the image's is Debian's 3.11).
"""
import contextlib
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time

MOUNT = Path("/sandbox-artex")
CONFIG = MOUNT / "config.json"
CA = MOUNT / "ca"  # the run's CA certificate (ca.pem) when the connection proxy runs, mounted by artex.py
BINARY = Path("/opt/artex/bin/artex")
SKILLS = Path("/opt/artex/skills")
POSTGRES = Path("/usr/lib/postgresql/15/bin")
SOCKET_DIR = Path("/tmp/artex-pg")
DATABASE = "artex"
DB_USER = "artex"
PASSWORD_ENV = "SANDBOX_AI_ARTEX_PASSWORD"  # artex.PASSWORD_ENV: podman create --env NAME, its value from podman's
PASSWORD_MIN = 8  # ARTEX's own (server/auth.go minPasswordRunes)
PG_WAIT = 90
CHROMIUM = "/usr/lib/chromium/chromium"
# What ARTEX's environment holds besides its LLM's and the CA's (environment()): nothing of the container's.
FIXED = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "LANG": "C.UTF-8",
    # norma installs ripgrep with npm when rg is missing (tool/ripgrep.go); the image has rg, and this keeps it so.
    "NORMA_RIPGREP_NO_INSTALL": "1",
    # Nothing that reports usage: Next.js (only at the build), and the general switch some tools honour.
    "NEXT_TELEMETRY_DISABLED": "1",
    "DO_NOT_TRACK": "1",
    # The image's Playwright MCP and CLI on the image's Chromium, headless; Chromium's own sandbox needs user
    # namespaces the container does not give (the container is the boundary, with sandbox-ai's around it).
    "PLAYWRIGHT_MCP_EXECUTABLE_PATH": CHROMIUM,
    "PLAYWRIGHT_MCP_HEADLESS": "1",
    "PLAYWRIGHT_MCP_SANDBOX": "0",
    "PLAYWRIGHT_BROWSERS_PATH": "/nonexistent",  # never a browser download of Playwright's own
    "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD": "1",
}
# What sandbox-ai's start may give ARTEX of its LLM (artex.llm_environment): placeholders and addresses only.
LLM_KEYS = ("ARTEX_LLM_PROVIDER", "ARTEX_LLM_MODEL", "ARTEX_LLM_BASE_URL", "ANTHROPIC_API_KEY", "OPENAI_API_KEY")


def say(text):
    print(f"sandbox-ai artex: {text}", file=sys.stderr, flush=True)


def load_config(path=CONFIG):
    config = json.loads(Path(path).read_text())
    for key in ("port", "proxy_port"):
        if not isinstance(config.get(key), int) or not 1 <= config[key] <= 65535:
            raise ValueError(f"config.json: {key} is no port")
    if not isinstance(config.get("data"), str) or not config["data"].startswith("/"):
        raise ValueError("config.json: data is no absolute path")
    llm = config.get("llm") or {}
    if not isinstance(llm, dict) or any(key not in LLM_KEYS or not isinstance(value, str) for key, value in llm.items()):
        raise ValueError(f"config.json: llm holds only {', '.join(LLM_KEYS)}")
    return config


def folders(data):
    """The data folder's parts, made (0700) when missing."""
    data = Path(data)
    found = {name: data / name for name in ("pg", "data", "keys", "skills", "home")}
    for path in found.values():
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return found


def seed_skills(target, source=None):
    """ARTEX's skills from the image into the data folder, once (an emptied folder stays as you left it then)."""
    target, source = Path(target), Path(source or SKILLS)
    marker = target / ".sandbox-ai-seeded"
    if marker.exists() or not source.is_dir():
        return False
    for entry in source.iterdir():
        destination = target / entry.name
        if destination.exists():
            continue
        if entry.is_dir():
            shutil.copytree(entry, destination, symlinks=True)
        else:
            shutil.copy2(entry, destination)
    marker.touch()
    return True


def dsn(socket_dir):
    """ARTEX_PG_DSN: PostgreSQL over its Unix socket (pgx takes a host that is a folder as one)."""
    return f"host={socket_dir} user={DB_USER} dbname={DATABASE} sslmode=disable"


def environment(config, socket_dir=None, ca_dir=CA):
    """ARTEX's whole environment: FIXED, its database, its folders, its LLM's placeholders and the run's CA."""
    parts = {name: Path(config["data"]) / name for name in ("data", "keys", "skills", "home")}
    env = dict(FIXED)
    env.update({"HOME": str(parts["home"]), "ARTEX_PG_DSN": dsn(socket_dir or SOCKET_DIR),
                "ARTEX_SKILL_DIR": str(parts["skills"]), "ARTEX_CONFIG": str(parts["home"] / "config.json")})
    env.update(config.get("llm") or {})
    ca = Path(ca_dir) / "ca.pem"
    if ca.is_file():
        # Go reads the system's bundle and every file of SSL_CERT_DIR; Node and Python's tools are told the same.
        env.update({"SSL_CERT_FILE": "/etc/ssl/certs/ca-certificates.crt", "SSL_CERT_DIR": f"/etc/ssl/certs:{ca_dir}",
                    "NODE_EXTRA_CA_CERTS": str(ca)})
    return env


def command(config, data_parts, password_fd):
    """ARTEX's command line: both listeners on the loopback, the password through the inherited pipe."""
    return [str(BINARY), "-addr", f"127.0.0.1:{config['port']}", "-proxy", f"127.0.0.1:{config['proxy_port']}",
            "-data", str(data_parts["data"]), "-keys", str(data_parts["keys"]),
            "-admin-password-file", f"/dev/fd/{password_fd}"]


def init_cluster(pgdata, run=subprocess.run):
    """PostgreSQL's cluster in PGDATA, once: its superuser this user (artex), only local connections, trusted
    (the socket's folder is this user's alone)."""
    if (Path(pgdata) / "PG_VERSION").exists():
        return False
    done = run([str(POSTGRES / "initdb"), "-D", str(pgdata), "-U", DB_USER, "--auth=trust", "--encoding=UTF8",
                "--locale=C.UTF-8", "--no-instructions"], stdin=subprocess.DEVNULL, capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError(f"initdb failed: {(done.stderr or done.stdout or '').strip()[-400:]}")
    return True


def postgres_command(pgdata, socket_dir):
    return [str(POSTGRES / "postgres"), "-D", str(pgdata), "-k", str(socket_dir), "-c", "listen_addresses=",
            "-c", "unix_socket_permissions=0700", "-c", "dynamic_shared_memory_type=mmap",
            "-c", "logging_collector=off", "-c", "max_connections=60"]


def psql(sql, socket_dir, database="postgres", run=subprocess.run):
    return run([str(POSTGRES / "psql"), "-h", str(socket_dir), "-U", DB_USER, "-d", database, "-Atqc", sql],
               stdin=subprocess.DEVNULL, capture_output=True, text=True)


def await_postgres(process, socket_dir, run=subprocess.run, sleep=time.sleep, clock=time.monotonic,
                   wait=PG_WAIT):
    """PostgreSQL answers on its socket (within WAIT seconds), and the database ARTEX uses is there."""
    end = clock() + wait
    while True:
        if process.poll() is not None:
            raise RuntimeError(f"PostgreSQL ended at its start (exit status {process.returncode})")
        if (Path(socket_dir) / ".s.PGSQL.5432").exists() and psql("SELECT 1", socket_dir, run=run).returncode == 0:
            break
        if clock() > end:
            raise RuntimeError(f"PostgreSQL did not answer within {wait} s")
        sleep(0.5)
    found = psql(f"SELECT 1 FROM pg_database WHERE datname = '{DATABASE}'", socket_dir, run=run)
    if found.returncode == 0 and found.stdout.strip() == "1":
        return False
    made = run([str(POSTGRES / "createdb"), "-h", str(socket_dir), "-U", DB_USER, DATABASE],
               stdin=subprocess.DEVNULL, capture_output=True, text=True)
    if made.returncode != 0:
        raise RuntimeError(f"createdb failed: {(made.stderr or '').strip()[-400:]}")
    return True


def take_password(environ):
    """The password sandbox-ai chose, taken out of ENVIRON (never handed on); ValueError when there is none."""
    password = environ.pop(PASSWORD_ENV, "") or ""
    if len(password) < PASSWORD_MIN or "\n" in password or "\r" in password:
        raise ValueError(f"no usable admin password in {PASSWORD_ENV}")
    return password


def password_pipe(password):
    """A pipe holding PASSWORD (then closed for writing): its read end, for ARTEX's -admin-password-file."""
    read, write = os.pipe()
    try:
        os.write(write, password.encode())
    finally:
        os.close(write)
    return read


def stop(process, how=signal.SIGTERM, wait=20):
    if process is None or process.poll() is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        process.send_signal(how)
    try:
        process.wait(timeout=wait)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        process.wait()


def serve(config_path=CONFIG, environ=None, popen=subprocess.Popen, run=subprocess.run, sleep=time.sleep):
    environ = os.environ if environ is None else environ
    try:
        password = take_password(environ)
        config = load_config(config_path)
    except (OSError, ValueError) as exc:
        say(f"not started: {exc}")
        return 2
    parts = folders(config["data"])
    seed_skills(parts["skills"])
    sockets = SOCKET_DIR
    sockets.mkdir(mode=0o700, parents=True, exist_ok=True)
    postgres = artex = None
    ending = threading.Event()

    def end(_number, _frame):
        ending.set()
    signal.signal(signal.SIGTERM, end)
    signal.signal(signal.SIGINT, end)
    try:
        if init_cluster(parts["pg"], run=run):
            say("PostgreSQL's cluster made")
        postgres = popen(postgres_command(parts["pg"], sockets), stdin=subprocess.DEVNULL,
                         env={"PATH": FIXED["PATH"], "LANG": "C.UTF-8"})
        await_postgres(postgres, sockets, run=run, sleep=sleep)
        read = password_pipe(password)
        try:
            artex = popen(command(config, parts, read), stdin=subprocess.DEVNULL, env=environment(config, sockets),
                          cwd=str(parts["home"]), pass_fds=(read,))
        finally:
            os.close(read)
        say(f"ARTEX started on 127.0.0.1:{config['port']} (its recording proxy on 127.0.0.1:{config['proxy_port']})")
        while not ending.is_set() and artex.poll() is None and postgres.poll() is None:
            sleep(0.5)
        if artex.poll() is not None and not ending.is_set():
            say(f"ARTEX ended (exit status {artex.returncode})")
        elif postgres.poll() is not None and not ending.is_set():
            say(f"PostgreSQL ended (exit status {postgres.returncode})")
        if artex.poll() is not None and not ending.is_set():
            return artex.returncode or 0
        return 0
    except (OSError, RuntimeError) as exc:
        say(f"not started: {exc}")
        return 1
    finally:
        stop(artex)
        stop(postgres, signal.SIGINT)  # PostgreSQL's fast shutdown


def connect(config_path=CONFIG, stdin=0, stdout=1, create=socket.create_connection):
    """One connection of the console's forward: standard input to ARTEX's console, its answer to standard output."""
    try:
        port = load_config(config_path)["port"]
        sock = create(("127.0.0.1", port), timeout=10)
    except (OSError, ValueError) as exc:
        say(f"no connection to the console: {exc}")
        return 1
    sock.settimeout(None)

    def upstream():
        try:
            while True:
                data = os.read(stdin, 65536)
                if not data:
                    break
                sock.sendall(data)
        except OSError:
            pass
        with contextlib.suppress(OSError):
            sock.shutdown(socket.SHUT_WR)
    threading.Thread(target=upstream, daemon=True).start()
    try:
        while True:
            data = sock.recv(65536)
            if not data:
                break
            view = memoryview(data)
            while view:
                view = view[os.write(stdout, view):]
    except OSError:
        pass
    finally:
        sock.close()
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv in ([], ["serve"]):
        return serve()
    if argv == ["connect"]:
        return connect()
    say("usage: artex_runtime.py [serve|connect]")
    return 2


if __name__ == "__main__":
    sys.exit(main())
