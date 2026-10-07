#!/usr/bin/env python3
"""A minimal Compose implementation for Apple `container`.

Apple ships no compose support -- `container compose` errors out, and the
community plugins do not implement healthcheck-gated `depends_on`, which a
stack of services that connect to each other at startup needs: a service
started before the one it depends on is healthy connects to nothing. So this
reads docker-compose.yml itself and drives the `container` CLI, keeping the
compose file the single source of truth for both runtimes.

Only the subset of Compose that real stacks have needed so far is
implemented. Where `container` cannot express something, the compose file
carries an `x-container:` block that is merged over the service (Compose
ignores `x-*` extension fields, so `docker compose config` stays valid).

Behaviours verified empirically against container 0.12.1 and 1.4.1 on macOS
(see RUNTIME NOTES below); each one is the reason for a specific decision here.

Originally extracted from HordiaLabs/scraper-deploy, whose docker-compose.yml
is still the reference stack it is exercised against; the examples in the
notes name that stack's services.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

# ── RUNTIME NOTES (container 0.12.1, macOS 26.6) ─────────────────
#
# Verified against scraper-deploy's docker-compose.yml, the reference stack.
# (An earlier revision carried these notes over verbatim from the repo this
# was adapted from, and they referenced services — garage-init, ferretdb —
# that did not exist there. Every note below names a service that does.)
#
# 1. Named volumes work: `-v <name>:/path` after `container volume create`.
# 2. Single-file bind mounts work; monitoring/clickhouse-prometheus.xml mounts
#    at /etc/clickhouse-server/config.d/prometheus.xml.
# 3. OVERLAPPING bind sources are silently broken: mounting a directory and a
#    file inside it drops the *directory* mount entirely, in either argument
#    order, with no error. Nothing in this compose file does that today; the
#    `x-container` merge below exists to express a fix if something starts to.
# 4. `container exec` works, so healthchecks declared as exec-form CMD can be
#    polled. Confirmed: `container exec <valkey> valkey-cli ping` -> PONG.
# 5. Published ports (-p) work ONLY on the `default` network. On a
#    user-defined network the host socket listens, accepts, then hangs — so
#    `ports:` is applied on the Docker path only.
# 6. The host can reach containers directly by IP on a user-defined network
#    with no publishing, so isolation does not cost host access.
# 7. Container-to-container name resolution needs a DNS domain:
#    `sudo container system dns create <domain>` plus
#    `container system property set dns.domain <domain>`. The domain is global
#    to `container`, not per-project.
# 8. Named volumes are formatted block devices, so they are NOT empty on first
#    use — every one arrives containing lost+found. Docker's named volumes are
#    plain directories and start empty.
#
#    This bites postgres here, and the mechanism is worth stating exactly
#    because it is not the obvious one. postgres:18 refuses to start when its
#    mount looks like it already holds data:
#
#        Error: ... there appears to be PostgreSQL data in:
#          /var/lib/postgresql/data (unused mount/volume)
#
#    lost+found is enough to trip that check. Under Docker the volume starts
#    empty, so it never fires — which is why this is a container-only problem
#    and is fixed with a container-only override rather than by changing the
#    mount for everyone. docker-compose.yml's postgres service carries an
#    `x-container:` block setting PGDATA to a subdirectory of the mount;
#    Compose ignores `x-*`, so `docker compose config` stays valid and the
#    Docker path is untouched. Verified: with PGDATA=/var/lib/postgresql/data/
#    pgdata the server reports "database system is ready to accept
#    connections" on a fresh volume.
#
#    ClickHouse's data volume is unaffected — it does not inspect its mount
#    for pre-existing contents the way the postgres entrypoint does.

# The file names `docker compose` itself looks for, in its order of preference.
COMPOSE_FILE_CANDIDATES = ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")


def default_compose_file() -> Path:
    """Resolve the compose file the way `docker compose` does.

    `-f` (handled in parse_cli) wins. Otherwise $RT_COMPOSE_FILE -- the
    variable scraper-deploy's runtime.sh uses so both runtimes can be pointed
    at another stack by one name -- then Compose's own $COMPOSE_FILE, then the
    first of Compose's default file names in the current directory. When none
    exists, docker-compose.yml is reported as the missing file.
    """
    for var in ("RT_COMPOSE_FILE", "COMPOSE_FILE"):
        if os.environ.get(var):
            return Path(os.environ[var])
    cwd = Path.cwd()
    for name in COMPOSE_FILE_CANDIDATES:
        if (cwd / name).is_file():
            return cwd / name
    return cwd / "docker-compose.yml"


COMPOSE_FILE = default_compose_file()

# Set by `container system property set dns.domain <domain>`. Containers are
# started with --dns-domain so both peers and the host can resolve them by
# name; without it there is no name resolution at all (note 7). The
# environment variable short-circuits the lookup, for tests and for hosts
# where several domains exist.
DNS_DOMAIN_ENV = "CONTAINER_COMPOSE_DNS_DOMAIN"

# Active --profile flags, populated from argv before the model is loaded.
# Module-level because load_model() is reached through several call paths and
# threading it through all of them would be noise.
PROFILE_ARGS: list[str] = []


def die(msg: str, code: int = 1) -> None:
    print(f"container-compose: {msg}", file=sys.stderr)
    raise SystemExit(code)


# ── Interpolation ────────────────────────────────────────────────
# Compose-compatible ${VAR}, ${VAR:-default}, ${VAR-default},
# ${VAR:?error}, ${VAR?error}, $VAR, and $$ -> literal $.
#
# The `:?` form is implemented for Compose parity, but note that this repo's
# docker-compose.yml does not currently use it: POSTGRES_PASSWORD and
# CLICKHOUSE_PASSWORD are plain `${VAR}`, so an unset one interpolates to an
# empty string rather than aborting. scripts/check-env.sh is what actually
# catches that, before `make up` runs. (An earlier revision of this comment
# claimed the file guarded "all five dev credentials" with `:?` — carried over
# from the repo this was adapted from, and never true here.)

def _match_brace(value: str, start: int) -> int:
    """Index of the `}` closing the `${` that starts at `start`, or -1.

    Written by hand because the default of a ${VAR:-default} may itself contain
    a ${...}, and a regex cannot match balanced braces. This stack does exactly
    that in several places, e.g.

        ${DATABASE_URL:-postgres://scraper:${POSTGRES_PASSWORD}@postgres:5432/scraper}

    A `[^}]*` arg pattern stops at the first `}` and yields
    `postgres://scraper:${POSTGRES_PASSWORD` — a silently wrong DSN handed to
    every database-backed service, which is worse than a parse error because
    the stack starts and then fails to connect.
    """
    # start is the index of '{'. Begin the scan ON it so it increments depth —
    # starting past it leaves depth at 0 and the first inner '}' looks like the
    # match, which is how this returned the inner brace on the very first
    # nested value it was given.
    depth = 0
    i = start
    while i < len(value):
        c = value[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _resolve(name: str, sep: str | None, arg: str, env: dict[str, str],
             where: str) -> str:
    raw = env.get(name)
    if sep is None:
        return raw if raw is not None else ""
    # A leading ':' makes an empty value count as unset, matching Compose.
    unset = raw is None or (sep.startswith(":") and raw == "")
    if sep.endswith("?"):
        if unset:
            die(f"{where}: {arg or f'{name} is required'}")
        return raw or ""
    return arg if unset else (raw or "")


def interpolate(value: str, env: dict[str, str], where: str) -> str:
    out: list[str] = []
    i = 0
    n = len(value)
    while i < n:
        c = value[i]
        if c != "$":
            out.append(c)
            i += 1
            continue
        if i + 1 < n and value[i + 1] == "$":  # $$ escapes a literal $
            out.append("$")
            i += 2
            continue
        if i + 1 < n and value[i + 1] == "{":
            close = _match_brace(value, i + 1)
            if close == -1:
                die(f"{where}: unbalanced ${{ in {value!r}")
            body = value[i + 2:close]
            m = re.match(r"([A-Za-z_][A-Za-z0-9_]*)(?:(:?[-?])([\s\S]*))?$", body)
            if not m:
                # Not a variable reference we understand; emit verbatim rather
                # than silently dropping it.
                out.append(value[i:close + 1])
                i = close + 1
                continue
            name, sep, arg = m.group(1), m.group(2), m.group(3) or ""
            # The default may itself contain references, so resolve it first.
            arg = interpolate(arg, env, where) if arg else ""
            out.append(_resolve(name, sep, arg, env, where))
            i = close + 1
            continue
        m = re.match(r"[A-Za-z_][A-Za-z0-9_]*", value[i + 1:])
        if m:
            out.append(_resolve(m.group(0), None, "", env, where))
            i += 1 + m.end()
            continue
        out.append(c)
        i += 1
    return "".join(out)


def interpolate_tree(node: Any, env: dict[str, str], where: str) -> Any:
    if isinstance(node, str):
        return interpolate(node, env, where)
    if isinstance(node, list):
        return [interpolate_tree(v, env, where) for v in node]
    if isinstance(node, dict):
        return {k: interpolate_tree(v, env, f"{where}.{k}") for k, v in node.items()}
    return node


# ── Loading ──────────────────────────────────────────────────────


def read_env_file(path: Path) -> dict[str, str]:
    """Parse a KEY=VALUE file the way Compose reads .env: literally, no
    expansion, '#' comments and blank lines skipped."""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        out[key.strip()] = val.strip()
    return out


def build_env() -> dict[str, str]:
    """.env, then the real environment.

    Shell variables win over the file, which is what Compose itself does.

    Read from the COMPOSE FILE's own directory, not this checkout's. That is
    what `docker compose -f <path>` does — the project directory is the compose
    file's directory — and COMPOSE_FILE is overridable via RT_COMPOSE_FILE
    precisely so the tools can be pointed at another stack. Reading from the
    checkout the script lived in instead made the two runtimes disagree
    silently: Docker would take the target stack's POSTGRES_PASSWORD while this
    translator took the checkout's, with no error either way. `resolve_bind_source()` already resolves
    relative to the compose file, so this was the odd one out.

    `.env.defaults` is also read if present, purely for parity with the repo
    this was adapted from — this one has no such file, and read_env_file
    returns {} for a missing path, so it is a no-op here. Kept rather than
    removed because `docker compose` itself has no .env.defaults concept: if
    someone adds one expecting Compose to honour it, Compose will not, and the
    two runtimes would disagree. Nothing should start depending on it.
    """
    project_dir = COMPOSE_FILE.parent
    env = read_env_file(project_dir / ".env.defaults")
    env.update(read_env_file(project_dir / ".env"))
    env.update(os.environ)
    return env


def merge_container_overrides(name: str, svc: dict[str, Any]) -> dict[str, Any]:
    """Apply a service's `x-container:` block over its base definition.

    Mappings (`environment`) merge key by key, so an override that adjusts one
    variable does not silently drop the rest -- and a variable added to the
    base service later still reaches the container runtime. Lists and scalars
    (`volumes`, `entrypoint`) replace outright: an override exists precisely
    because the Docker value is wrong here, so appending to it would keep the
    broken half.
    """
    override = svc.pop("x-container", None)
    if not override:
        return svc
    if not isinstance(override, dict):
        die(f"service {name}: x-container must be a mapping")
    svc = dict(svc)
    for key, value in override.items():
        base = svc.get(key)
        if isinstance(base, dict) and isinstance(value, dict):
            merged = dict(base)
            merged.update(value)
            svc[key] = merged
        else:
            svc[key] = value
    return svc


def load_model(apply_profiles: bool = True) -> tuple[str, dict[str, dict[str, Any]], dict[str, Any], str]:
    try:
        import yaml
    except ModuleNotFoundError:
        die(
            "PyYAML is required to read the compose file.\n"
            "  It is a declared dependency, so this means the module was run from a\n"
            "  checkout rather than installed. Either `pip install .` (or `uv tool\n"
            "  install .`) in this repo, or `python3 -m pip install pyyaml`."
        )
    if not COMPOSE_FILE.is_file():
        die(f"compose file not found: {COMPOSE_FILE}")

    env = build_env()
    try:
        raw = yaml.safe_load(COMPOSE_FILE.read_text()) or {}
    except yaml.YAMLError as exc:
        # A malformed compose file is an ordinary user error — the pre-commit
        # hook exists to catch exactly this — so report it rather than dumping
        # a traceback that buries the line number in noise.
        die(f"{COMPOSE_FILE.name} is not valid YAML:\n  {exc}")
    doc = interpolate_tree(raw, env, COMPOSE_FILE.name)

    project = doc.get("name") or COMPOSE_FILE.parent.name
    services = {
        name: merge_container_overrides(name, dict(svc or {}))
        for name, svc in (doc.get("services") or {}).items()
    }
    if apply_profiles:
        services = select_profiles(services)
    volumes = doc.get("volumes") or {}
    network = f"{project}_default"
    for netname, net in (doc.get("networks") or {}).items():
        network = (net or {}).get("name") or netname
        break
    return project, services, volumes, network


# ── Profiles ─────────────────────────────────────────────────────
#
# The reference implementation this was adapted from had no profile support,
# because its stack had no profiles. This one is profile-driven: 17 of the 27
# services are gated, and starting all of them would bring up both monitoring
# backends at once and an S3 server nothing asked for.
#
# Compose semantics, reproduced: a service with no `profiles:` always starts; a
# service with `profiles:` starts only when at least one of them is active.
# Profiles are named with --profile (repeatable) or COMPOSE_PROFILES
# (comma-separated), matching `docker compose`, so the Makefile passes the same
# flags to either runtime.


def active_profiles(argv_profiles: list[str]) -> set[str]:
    names = set(argv_profiles)
    env_val = os.environ.get("COMPOSE_PROFILES", "")
    names |= {p.strip() for p in env_val.split(",") if p.strip()}
    return names


def select_profiles(services: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    active = active_profiles(PROFILE_ARGS)
    selected = {}
    for name, svc in services.items():
        want = svc.get("profiles") or []
        if not want or (set(want) & active):
            selected[name] = svc
    return selected


def check_runtime_support(services: dict[str, dict[str, Any]]) -> None:
    """Refuse services Apple `container` cannot run, rather than failing
    obscurely mid-`up`.

    The typical case is monitoring: cadvisor and promtail bind-mount the host
    Docker socket (cadvisor also wants --privileged). There is no
    Apple-container equivalent: the socket is Docker's own API, and cAdvisor
    reads it to enumerate Docker containers. So this is a property of those
    images, not a gap in this translator, and no amount of work here would
    make them useful under a different runtime.
    """
    blocked = []
    for name, svc in services.items():
        mounts = " ".join(str(v) for v in (svc.get("volumes") or []))
        if "docker.sock" in mounts:
            blocked.append((name, "bind-mounts the Docker socket"))
        elif svc.get("privileged"):
            blocked.append((name, "requires --privileged"))
    if blocked:
        lines = "\n".join(f"    {n}: {why}" for n, why in blocked)
        die(
            "these services cannot run under Apple `container`:\n"
            f"{lines}\n"
            "  They read Docker's own API, so there is no equivalent here.\n"
            "  Run them under Docker, or keep them behind a compose profile that\n"
            "  is not selected on this runtime."
        )


# ── Naming ───────────────────────────────────────────────────────


# Matches the failure `container` reports when the requested platform is not in
# the image's manifest list. Observed verbatim on container 0.12.1:
#
#   Error: unsupported platform Platform(osVersion: nil, osFeatures: nil,
#          variant: Optional("v8"), _rawOS: "linux", _rawArch: "arm64")
#
# Deliberately narrow. Every other failure — a registry timeout, a rate limit,
# an auth hiccup, an unknown tag — must NOT be treated as "this platform is
# unavailable", because the remedy for that (pulling with no --platform) fetches
# every entry in the manifest list. On a multi-arch image that turns a transient
# blip into the multi-gigabyte download --platform exists to prevent.
_UNSUPPORTED_PLATFORM = re.compile(r"unsupported platform", re.IGNORECASE)


def pull_image(argv: list[str]) -> tuple[int, str]:
    """Run a pull, streaming its output through while capturing it.

    `container` writes BOTH progress and errors to stderr, so plain
    capture_output would leave a multi-minute pull looking hung. This tees:
    bytes go to our stderr as they arrive, and are accumulated so the caller can
    tell a platform failure from every other kind.
    """
    proc = subprocess.Popen(argv, stderr=subprocess.PIPE)
    captured = bytearray()
    if proc.stderr is not None:
        while True:
            # read1, not readline: the progress display overwrites itself with
            # carriage returns, so line-buffering would hold it back until the
            # pull finished.
            chunk = proc.stderr.read1(4096)
            if not chunk:
                break
            sys.stderr.buffer.write(chunk)
            sys.stderr.buffer.flush()
            captured += chunk
    proc.wait()
    return proc.returncode, captured.decode("utf-8", "replace")


def host_platform() -> str:
    """The platform `docker compose pull` would fetch: the host's own.

    `container image pull` with no --platform fetches EVERY platform in the
    manifest list — pulling linux/ppc64le and friends for an image that will
    only ever run as arm64 here. That is minutes of bandwidth and gigabytes of
    disk per image across a 27-service stack.
    """
    machine = platform.machine().lower()
    arch = {"arm64": "arm64", "aarch64": "arm64",
            "x86_64": "amd64", "amd64": "amd64"}.get(machine, machine)
    return f"linux/{arch}"


def parse_size(value: str | int) -> int:
    """Compose byte sizes ("2gb", "3g", "512m", 1048576) -> bytes.

    Compose accepts a bare integer as bytes and the suffixes b/k/m/g/t, with an
    optional trailing "b" ("2gb" and "2g" are the same thing).
    """
    if isinstance(value, int):
        return value
    text = str(value).strip().lower()
    if text.isdigit():
        return int(text)
    if not text:
        # An empty value reaches here from `shm_size: ""`, or from a
        # ${VAR}-interpolated size whose variable is unset. Without this the
        # indexing below raises IndexError — the confusing crash this function
        # exists to replace.
        die(f"unrecognised size {value!r} (expected e.g. 2gb, 512m, or bytes)")
    units = {"b": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3, "t": 1024 ** 4}
    body = text[:-1] if text.endswith("b") and len(text) > 1 and not text[-2].isdigit() else text
    unit = body[-1]
    if unit not in units:
        die(f"unrecognised size {value!r} (expected e.g. 2gb, 512m, or bytes)")
    try:
        amount = float(body[:-1])
    except ValueError:
        die(f"unrecognised size {value!r} (expected e.g. 2gb, 512m, or bytes)")
    return int(amount * units[unit])


def container_name(service: str, svc: dict[str, Any]) -> str:
    return svc.get("container_name") or service


def volume_name(project: str, declared: str) -> str:
    """Compose prefixes declared volumes with the project name. Match it, so
    the names match what `docker compose` would create, so a stack moved
    between runtimes finds the same data."""
    return f"{project}_{declared}"


def resolve_bind_source(src: str) -> str:
    """Bind sources in the compose file are relative to the compose file's own
    directory, per the Compose spec."""
    return str((COMPOSE_FILE.parent / src).resolve())


def is_bind(src: str) -> bool:
    return src.startswith((".", "/", "~"))


# ── Command construction ─────────────────────────────────────────


def build_run_argv(
    project: str,
    service: str,
    svc: dict[str, Any],
    network: str,
    dns_domain: str | None,
) -> list[str]:
    name = container_name(service, svc)
    argv = ["container", "run", "--detach", "--name", name]

    if network:
        argv += ["--network", network]
    if dns_domain:
        argv += ["--dns-domain", dns_domain]

    env = svc.get("environment") or {}
    if isinstance(env, list):
        # Compose's list form allows a bare `VAR` with no `=`, meaning "pass
        # the host's value through". Splitting unconditionally gave a 1-element
        # list and a ValueError about dictionary update sequences — a confusing
        # crash for the next person to add one, rather than this file's usual
        # "that part of Compose is not implemented" message.
        pairs: dict[str, str] = {}
        for item in env:
            if "=" in item:
                key, _, val = item.partition("=")
                pairs[key] = val
            elif item in os.environ:
                pairs[item] = os.environ[item]
            # A bare VAR that is unset on the host is dropped, which is what
            # `docker compose` does: it passes no value rather than an empty
            # one, so the image's own default survives.
        env = pairs
    for key, val in env.items():
        argv += ["--env", f"{key}={val}"]

    for spec in svc.get("volumes") or []:
        if not isinstance(spec, str):
            die(f"service {service}: only short-form volume syntax is supported")
        parts = spec.split(":")
        if len(parts) < 2:
            die(f"service {service}: malformed volume {spec!r}")
        src, target = parts[0], parts[1]
        mode = parts[2] if len(parts) > 2 else ""
        src = resolve_bind_source(src) if is_bind(src) else volume_name(project, src)
        argv += ["--volume", f"{src}:{target}" + (f":{mode}" if mode else "")]

    # Ports are deliberately NOT published. Publishing only works on the
    # `default` network (note 5); this stack runs on its own network so the
    # services stay isolated, and the host reaches them by name through the
    # DNS domain (note 6/7) instead of through 127.0.0.1.
    #
    # `ports:` in the compose file therefore applies to the Docker path only.

    # A declared platform must reach `container run`, not just `pull`.
    #
    # Verified on container 0.12.1: running an amd64-only image on arm64 fails
    # with the same "unsupported platform" error whether or not it is already
    # pulled — a cached image is NOT enough, `container run` still resolves the
    # platform itself and reports `Error: platform linux/arm64`. Since `make up`
    # does not call `pull` first, every one of the nine amd64-only services
    # failed at `up` without this.
    #
    # Only passed when DECLARED. Forcing the host's platform on an undeclared
    # service would break multi-arch images that work today by letting
    # `container` choose, and `start()` has no retry to fall back on.
    declared_platform = svc.get("platform")
    if declared_platform:
        argv += ["--platform", str(declared_platform)]

    # shm_size -> a sized tmpfs at /dev/shm.
    #
    # This one is not cosmetic. Verified against container 0.12.1: the default
    # /dev/shm is 64M, and neither `--tmpfs /dev/shm:size=2g` nor
    # `--tmpfs /dev/shm,size=2g` honours the size (both leave it at 64M) —
    # only the --mount form does. Headless Chromium and Firefox are precisely
    # the workload that dies on an undersized /dev/shm, with a renderer crash
    # that looks nothing like a runtime-translation problem, and
    # fetcher-playwright/fetcher-camoufox both ask for 2gb.
    shm = svc.get("shm_size")
    if shm is not None:
        argv += ["--mount",
                 f"type=tmpfs,destination=/dev/shm,size={parse_size(shm)}"]

    # deploy.resources.limits -> --cpus / --memory. `docker compose up` applies
    # these outside swarm mode, so they are enforced under Docker today; left
    # untranslated they would be silently absent here.
    limits = (((svc.get("deploy") or {}).get("resources") or {}).get("limits") or {})
    if limits.get("cpus") is not None:
        # Compose allows fractional CPUs ("0.5"); container takes a whole
        # number, so round up rather than down — a limit that silently became
        # zero, or a fraction rejected at run time, is worse than a slightly
        # generous one.
        #
        # Measured on container 0.12.1: the guest reports N+1 CPUs for
        # `--cpus N` (1->2, 2->3, 4->5), consistently. The limit is applied;
        # the VM just carries one more than it was asked for. Noted so the
        # next person to run `nproc` in a container does not chase it.
        argv += ["--cpus", str(max(1, math.ceil(float(limits["cpus"]))))]
    if limits.get("memory") is not None:
        argv += ["--memory", str(parse_size(limits["memory"]))]

    entrypoint = svc.get("entrypoint")
    trailing: list[str] = []
    if entrypoint:
        if isinstance(entrypoint, str):
            entrypoint = ["/bin/sh", "-c", entrypoint]
        argv += ["--entrypoint", entrypoint[0]]
        trailing += entrypoint[1:]

    argv.append(svc["image"])

    command = svc.get("command")
    if command:
        # Compose word-splits a string-form `command:` — it does not wrap it in
        # a shell and does not pass it as one literal argument. Passing it
        # whole gave cloudflared a single argv element
        # "tunnel --no-autoupdate run" instead of three, which it cannot parse
        # as a subcommand plus flags, so `make up-tunnel` started a broken
        # container. shlex, not str.split, so a quoted argument survives.
        trailing += shlex.split(command) if isinstance(command, str) else list(command)
    argv += trailing
    return argv


def healthcheck_argv(svc: dict[str, Any]) -> list[str] | None:
    """Translate a compose healthcheck into an argv for `container exec`.

    Both forms appear in docker-compose.yml and both matter: valkey and
    clickhouse use exec-form CMD, while the postgres and plugin probes are
    CMD-SHELL and need a shell (`pg_isready -U scraper`, `wget -q -O- .../health
    || exit 1`).
    """
    hc = svc.get("healthcheck") or {}
    if hc.get("disable"):
        return None
    test = hc.get("test")
    if not test:
        return None
    if isinstance(test, str):
        return ["/bin/sh", "-c", test]
    kind, rest = test[0], list(test[1:])
    if kind == "NONE":
        return None
    if kind == "CMD-SHELL":
        return ["/bin/sh", "-c", rest[0]]
    if kind == "CMD":
        return rest
    return ["/bin/sh", "-c", " ".join(test)]


def _seconds(value: Any, default: float) -> float:
    """Parse a compose duration (10s, 1m30s, 500ms) into seconds."""
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    total, num = 0.0, ""
    units = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}
    for token, mult in re.findall(r"(\d+)(ms|s|m|h)", str(value)):
        total += float(token) * units[mult]
        num = token
    return total if total or num else default


def health_budget(svc: dict[str, Any]) -> float:
    """How long to wait for a service, derived from its own declared probe so
    the compose file stays the single source of truth for timing."""
    hc = svc.get("healthcheck") or {}
    interval = _seconds(hc.get("interval"), 5.0)
    retries = int(hc.get("retries", 5) or 5)
    start_period = _seconds(hc.get("start_period"), 0.0)
    # Images have to be pulled and a VM booted before the first probe can pass,
    # so keep a generous floor rather than trusting the declared budget alone.
    return max(120.0, start_period + interval * retries * 2)


# ── Dependency ordering ──────────────────────────────────────────


def depends_map(svc: dict[str, Any], present: set[str]) -> dict[str, str]:
    """Normalise both `depends_on` forms to {dependency: condition}.

    `present` is REQUIRED, not defaulted, and that is deliberate. It was
    optional at first, and the one call site that forgot it — `up()`, the path
    that actually starts containers — silently treated every `required: false`
    dependency as mandatory and crashed with KeyError on the two flagship
    cases: `make up-external` (no embedded profiles, so valkey/postgres are
    absent) and `make up-direct` (no proxy profile, so the router is absent).
    `plan()` passed it and was correct, so the whole test suite passed while
    `up` was broken. Making it required turns that into a TypeError at the call
    site instead of a runtime crash in front of a user.

    `required: false` (Compose v2.20+) is honoured: a dependency filtered out
    by profile selection is dropped rather than treated as a dangling
    reference. This stack relies on it heavily —
    every plugin declares `required: false` on postgres/valkey/clickhouse so
    the same file works against embedded *and* externally-managed instances.
    Without this, selecting no embedded-db profile would fail the whole plan
    on "depends_on unknown service", which is precisely the configuration
    `make up-external` exists to run.

    A dependency that is absent and NOT marked optional stays in the map, so
    the dangling-reference check below still catches a genuine typo.
    """
    dep = svc.get("depends_on") or {}
    if isinstance(dep, list):
        return {name: "service_started" for name in dep}
    out: dict[str, str] = {}
    for name, spec in dep.items():
        spec = spec or {}
        if name not in present and spec.get("required") is False:
            continue
        out[name] = spec.get("condition", "service_started")
    return out


def waves(services: dict[str, dict[str, Any]]) -> list[list[str]]:
    """Group services into start order. Everything in a wave can start in
    parallel; the next wave begins only once this one's conditions are met."""
    present = set(services)
    pending = {name: set(depends_map(svc, present)) for name, svc in services.items()}
    for name, deps in pending.items():
        unknown = deps - present
        if unknown:
            die(
                f"service {name}: depends_on unknown service(s) {sorted(unknown)}.\n"
                "  If those are profile-gated, either activate their profile or mark\n"
                "  the dependency `required: false` in docker-compose.yml."
            )

    out: list[list[str]] = []
    done: set[str] = set()
    while pending:
        wave = sorted(n for n, deps in pending.items() if deps <= done)
        if not wave:
            die(f"circular depends_on among {sorted(pending)}")
        out.append(wave)
        done |= set(wave)
        for name in wave:
            del pending[name]
    return out


# ── Engine ───────────────────────────────────────────────────────


class Engine:
    def __init__(self, dry_run: bool = False) -> None:
        if not dry_run and not shutil.which("container"):
            die("the `container` CLI was not found on PATH")
        self.dry_run = dry_run
        self.project, self.services, self.volumes, self.network = load_model()
        check_runtime_support(self.services)
        self.dns_domain = os.environ.get(DNS_DOMAIN_ENV) or self._system_dns_domain()

    # -- helpers ---------------------------------------------------

    def _system_dns_domain(self) -> str | None:
        """Read `dns.domain` from the container system properties.

        Without a registered domain there is no name resolution at all
        (note 7), and this stack's services address each other by name, so a
        missing domain is a setup error rather than a soft default.
        """
        if self.dry_run:
            return os.environ.get(DNS_DOMAIN_ENV)

        # Three sources, because the CLI changed shape under us and the first
        # one silently stopped answering.
        #
        # container 0.12.x printed an aligned table:
        #     dns.domain   String   sproncy   If defined, ...
        # 1.4.x prints TOML, and — measured on 1.4.1 — emits an EMPTY `[dns]`
        # section even when the domain IS set. So the original parse returned
        # None on a correctly configured host, and `require_dns()` then refused
        # to start the stack. A missing domain and an unreadable one look
        # identical from one source, which is why there are three.
        for value in (
            self._dns_from_property_table(),
            self._dns_from_dns_list(),
            self._dns_from_user_defaults(),
        ):
            if value:
                return value
        return None

    def _dns_from_property_table(self) -> str | None:
        """container 0.12.x: an aligned table of properties."""
        out = self._capture(["container", "system", "property", "list"])
        for line in out.splitlines():
            if line.startswith("dns.domain"):
                parts = line.split()
                value = parts[2] if len(parts) > 2 else ""
                return None if value == "*undefined*" else value
        return None

    def _dns_from_dns_list(self) -> str | None:
        """container 1.x: `system dns list` prints the registered domains.

        Only trusted when there is exactly one. With several registered there
        is no way to tell from here which one `dns.domain` actually names, and
        guessing would attach containers to a domain nothing resolves.
        """
        out = self._capture(["container", "system", "dns", "list"])
        rows = [ln.strip() for ln in out.splitlines()[1:] if ln.strip()]
        return rows[0] if len(rows) == 1 else None

    def _dns_from_user_defaults(self) -> str | None:
        """The value itself, from where the CLI persists it.

        Last resort: it reads the same key `container system property list`
        claims to show, so it stays correct while that output is broken.
        """
        out = self._capture(
            ["defaults", "read", "com.apple.container.defaults", "dns.domain"]
        ).strip()
        return out or None

    def _capture(self, argv: list[str]) -> str:
        try:
            return subprocess.run(
                argv, check=False, capture_output=True, text=True
            ).stdout
        except OSError as exc:
            die(f"failed to run {argv[0]}: {exc}")
            return ""

    def _run(self, argv: list[str], check: bool = True, quiet: bool = False) -> int:
        """Run a `container` command.

        quiet=True drops stdout only. Most container subcommands echo the name
        of whatever they just acted on, which turns `make up` into a column of
        bare container names interleaved with our own progress lines. stderr is
        always kept -- that is where real failures surface.
        """
        if self.dry_run:
            print("  " + " ".join(shlex.quote(a) for a in argv))
            return 0
        proc = subprocess.run(
            argv, check=False, stdout=subprocess.DEVNULL if quiet else None
        )
        if check and proc.returncode != 0:
            die(f"command failed ({proc.returncode}): {' '.join(argv)}")
        return proc.returncode

    def state(self, name: str) -> str:
        """running | stopped | missing"""
        out = self._capture(["container", "inspect", name])
        if not out.strip():
            return "missing"
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            return "missing"
        if not data:
            return "missing"

        # `status` changed shape between CLI generations. 0.12.x returned a
        # plain string ("running"); 1.4.x returns an object:
        #
        #     "status": {"state": "running", "networks": [...], ...}
        #
        # Returning that object made every `state(...) == "running"` comparison
        # false while the container was demonstrably running — so `up` recreated
        # live containers and `make health` reported every embedded service as
        # external. A silently wrong answer, which is the failure mode this file
        # keeps having to defend against.
        status = data[0].get("status", "missing")
        if isinstance(status, dict):
            status = status.get("state", "missing")
        return status if isinstance(status, str) else "missing"

    def selected_names(self) -> dict[str, str]:
        """Service -> container name, for the PROFILE-SELECTED services only.

        Named `selected_names` rather than `names` deliberately. As `names()`
        it read like "the names", and three separate methods reached for it
        when they wanted every service — logs, then stop/rm, then ps — each
        shipping a bug where a profile-gated service looked absent because
        this invocation had not named its profile. The scope is now in the
        call, so choosing wrong requires saying so.

        Use this only for lifecycle operations that should honour the active
        profiles (`up`). Anything that asks "does this container exist" wants
        all_names().
        """
        return {n: container_name(n, s) for n, s in self.services.items()}

    def all_services(self) -> dict[str, dict[str, Any]]:
        """Every service DEFINITION in the compose file, profile-gated or not.

        `self.services` holds only the profile selection, so reaching into it
        for a gated service raises KeyError — the same trap that produced the
        logs/stop/rm/ps bugs. Anything that works off `all_names()` needs this
        rather than `self.services`.
        """
        _, services, _, _ = load_model(apply_profiles=False)
        return services

    def all_names(self) -> dict[str, str]:
        """Service -> container name for EVERY service in the compose file,
        profile-gated or not.

        Lifecycle operations (`up`, `down`) act on the selection; read-only
        ones (`logs`) should not, because the Makefile's `logs-%` target passes
        no --profile flags and would otherwise be unable to tail any gated
        service — valkey and postgres included.
        """
        _, services, _, _ = load_model(apply_profiles=False)
        return {n: container_name(n, s) for n, s in services.items()}

    def require_dns(self) -> None:
        if self.dns_domain:
            return
        die(
            "no container DNS domain is configured, so services cannot resolve\n"
            "  each other by name. Set one up once with:\n"
            "      sudo container system dns create scraper\n"
            "      container system property set dns.domain scraper"
        )

    # -- operations ------------------------------------------------

    def ensure_network(self) -> None:
        existing = self._capture(["container", "network", "ls"])
        if self.dry_run or self.network not in existing.split():
            self._run(["container", "network", "create", self.network],
                      check=False, quiet=True)

    def ensure_volumes(self) -> None:
        existing = self._capture(["container", "volume", "ls"]).split()
        for declared in self.volumes:
            name = volume_name(self.project, declared)
            if self.dry_run or name not in existing:
                self._run(["container", "volume", "create", name], check=False, quiet=True)

    def wait_healthy(self, service: str) -> None:
        svc = self.services[service]
        name = container_name(service, svc)
        probe = healthcheck_argv(svc)
        if probe is None:
            return
        budget = health_budget(svc)
        deadline = time.monotonic() + budget
        print(f"  waiting for {name} to become healthy (up to {int(budget)}s) ...")
        while time.monotonic() < deadline:
            if self.state(name) == "stopped":
                self.dump_logs(name)
                die(f"{name} exited before becoming healthy")
            probe_rc = subprocess.run(
                ["container", "exec", name, *probe],
                check=False, capture_output=True, text=True,
            ).returncode
            if probe_rc == 0:
                print(f"  {name} is healthy")
                return
            time.sleep(2)
        self.dump_logs(name)
        die(f"{name} did not become healthy within {int(budget)}s")

    def dump_logs(self, name: str, tail: int = 40) -> None:
        print(f"--- last {tail} log lines from {name} ---", file=sys.stderr)
        out = self._capture(["container", "logs", name])
        print("\n".join(out.splitlines()[-tail:]), file=sys.stderr)

    def up(self, only: list[str] | None = None) -> None:
        """`only` restarts a subset in place, for `restart <service>`.

        Dependency health gates are still honoured — a single service being
        recreated must still wait for valkey to be healthy — but services
        outside the subset are not started.
        """
        self.require_dns()
        self.ensure_network()
        self.ensure_volumes()

        ignored = sorted(n for n, s in self.services.items() if s.get("restart"))
        if ignored:
            print(
                f"note: `restart:` has no equivalent in container and is ignored "
                f"for {', '.join(ignored)}"
            )

        for wave in waves(self.services):
            for service in wave:
                for dep, condition in depends_map(
                    self.services[service], set(self.services)
                ).items():
                    if condition == "service_healthy":
                        self.wait_healthy(dep)
            for service in wave:
                if only is not None and service not in only:
                    continue
                self.start(service)

    def start(self, service: str) -> None:
        svc = self.services[service]
        name = container_name(service, svc)
        state = self.state(name)
        if state == "running":
            print(f"  {name} is already running")
            return
        if state != "missing":
            # A stopped container holds its name and its old configuration.
            # Recreate rather than restart so a compose edit actually takes
            # effect; all persistent state lives in named volumes.
            self._run(["container", "rm", name], check=False, quiet=True)
        argv = build_run_argv(
            self.project, service, svc, self.network, self.dns_domain
        )
        print(f"  starting {name}")
        rc = self._run(argv, check=False, quiet=True)
        if rc != 0:
            # An undeclared amd64-only image dies here with `container`'s own
            # terse "unsupported platform" line and no indication of the fix.
            # There is no retry to fall back on at this point — the wave
            # ordering has already run — so name the remedy instead.
            hint = ""
            if not svc.get("platform"):
                hint = (
                    f"\n  If {svc.get('image', service)} does not publish "
                    f"{host_platform()}, declare the one it does:\n"
                    f"      {service}:\n"
                    f"        platform: linux/amd64\n"
                    f"  in docker-compose.yml. `container run` resolves the "
                    f"platform itself, so pulling first does not help."
                )
            die(f"failed to start {name} ({rc}){hint}")

    def down(self, remove_volumes: bool = False) -> None:
        """Tear down the whole project, not just the profile selection.

        `docker compose down` removes every container belonging to the project
        regardless of which profiles are active — it works off the project
        label, not the selection. Matching that matters here because the
        network cannot be deleted while anything still references it:

            failed to delete network: cannot delete subnet
            scraper-deploy_default with referring containers:
            clickhouse, postgres, valkey

        which is what `container-compose.py down` (no --profile) did, leaving
        the stack half up and the error pointing at the network rather than at
        the containers that were never stopped.
        """
        for service, name in reversed(list(self.all_names().items())):
            if self.state(name) != "missing":
                print(f"  removing {name}")
                self._run(["container", "stop", name], check=False, quiet=True)
                self._run(["container", "rm", name], check=False, quiet=True)
        self._run(["container", "network", "rm", self.network], check=False, quiet=True)
        if remove_volumes:
            for declared in self.volumes:
                self._run(
                    ["container", "volume", "rm", volume_name(self.project, declared)],
                    check=False, quiet=True,
                )

    def restart(self, services: list[str] | None = None) -> None:
        """`docker compose restart [service...]`.

        With names, only those are recreated — the runbook's "upgrade a single
        service" and "rotate proxy credentials" steps both restart one service
        and must not cycle the whole stack. Explicit names resolve against the
        whole compose file (see `_selected`), since neither caller passes
        --profile.
        """
        if services:
            wanted = self._selected(services)
            # _selected resolves against the whole file, but self.services is
            # the profile SELECTION — so `restart valkey` with no --profile
            # removed the container and then started nothing, because up()'s
            # wave loop never saw valkey at all. Same trap as logs/stop/rm/ps
            # and pull: an explicitly named service must not need its profile
            # restated. Pull the missing definitions in first.
            missing = {n: d for n, d in self.all_services().items()
                       if n in wanted and n not in self.services}
            self.services.update(missing)
            for name in wanted.values():
                if self.state(name) == "running":
                    self._run(["container", "stop", name], check=False, quiet=True)
                if self.state(name) != "missing":
                    self._run(["container", "rm", name], check=False, quiet=True)
            # Recreated, not merely started, so a freshly pulled image is
            # actually picked up — the whole point of the upgrade recipe.
            self.up(only=list(wanted))
            return
        for service, name in self.selected_names().items():
            if self.state(name) == "running":
                self._run(["container", "stop", name], check=False, quiet=True)
        self.up()

    def _selected(self, services: list[str] | None) -> dict[str, str]:
        """Resolve an explicit service list, or the profile selection if none.

        An explicitly-named service is resolved against EVERY service in the
        compose file, not the profile-selected subset — matching
        `docker compose stop <service>`, which does not require you to re-state
        the profile that service belongs to.

        The Makefile depends on this. Its teardown targets name profile-gated
        services directly and pass no --profile:

            down-monitoring:  $(COMPOSE) stop prometheus alertmanager grafana …
            down-prometheus:  $(COMPOSE) stop prometheus

        Every one of those is gated behind `profiles: [monitoring]`, so
        resolving against the selection made all three die with "unknown
        service(s): prometheus" under this runtime. Same bug `logs()` documents
        below; stopping something is no more a reason to re-declare its profile
        than reading its logs is.
        """
        if not services:
            return self.selected_names()
        all_names = self.all_names()
        unknown = [s for s in services if s not in all_names]
        if unknown:
            die(
                f"unknown service(s): {', '.join(sorted(unknown))}\n"
                f"  known: {', '.join(sorted(all_names))}"
            )
        return {s: all_names[s] for s in services}

    def stop(self, services: list[str] | None = None) -> None:
        for name in self._selected(services).values():
            if self.state(name) == "running":
                self._run(["container", "stop", name], check=False, quiet=True)

    def rm(self, services: list[str] | None = None) -> None:
        for name in self._selected(services).values():
            if self.state(name) != "missing":
                self._run(["container", "stop", name], check=False, quiet=True)
                self._run(["container", "rm", name], check=False, quiet=True)

    def ps(self, status: str | None = None, services_only: bool = False) -> None:
        """`status` and `services_only` mirror `docker compose ps --status X
        --services`, which the Makefile's _check_embedded relies on to tell an
        embedded service from an external one.

        With --services the output is bare service names, one per line, so
        `| grep -qx postgres` works identically under both runtimes.
        """
        # Bound to a distinct name: `status` is reused below as the per-
        # container state, and shadowing the filter with it would silently
        # compare a value against itself.
        status_filter = status
        rows = [("NAME", "STATUS", "ADDRESS")]
        matched: list[str] = []
        # all_names, not selected_names: `docker compose ps` reports containers
        # that exist, regardless of the profile flags on THIS invocation. The
        # Makefile relies on exactly that — _check_embedded, psql and
        # clickhouse-shell all run `ps --status running --services` with no
        # --profile and grep for postgres/valkey/clickhouse, every one of which
        # is profile-gated. Filtering by the selection made that grep always
        # miss, so those targets reported embedded services as external even
        # immediately after `make up` started them.
        for service, name in self.all_names().items():
            out = self._capture(["container", "inspect", name])
            status, addr = "missing", "-"
            if out.strip():
                try:
                    data = json.loads(out)[0]
                    # Same shape change `state()` documents: 0.12.x had a
                    # string status and top-level `networks`; 1.4.x nests both
                    # under a status object. Reading the old shape here made
                    # `ps --status running --services` print NOTHING on a
                    # running stack, which is what `make health`, `make psql`
                    # and `make clickhouse-shell` all grep.
                    raw_status = data.get("status", "unknown")
                    if isinstance(raw_status, dict):
                        status = raw_status.get("state", "unknown")
                        nets = raw_status.get("networks") or []
                    else:
                        status = raw_status
                        nets = data.get("networks") or []
                    # `container inspect` reports ipv4Address (with a /prefix),
                    # not Docker's `address`. Only populated while running.
                    addr = (nets[0].get("ipv4Address") if nets else None) or "-"
                    addr = addr.split("/")[0]
                except (json.JSONDecodeError, IndexError, KeyError):
                    pass
            if status == "running" and self.dns_domain:
                addr = f"{name}.{self.dns_domain} ({addr})"
            if status_filter is not None and status != status_filter:
                continue
            matched.append(service)
            rows.append((name, status, addr))

        if services_only:
            # Service names, not container names: `docker compose ps
            # --services` prints the compose service, and the Makefile greps
            # for exactly that.
            for service in matched:
                print(service)
            return

        widths = [max(len(r[i]) for r in rows) for i in range(3)]
        for row in rows:
            print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())

    def pull(self, services: list[str] | None = None) -> None:
        """`docker compose pull` — fetch each service's image ahead of `up`.

        The Makefile's `pull` target routed through $(COMPOSE) with no
        subcommand here, so `make pull` died at argparse with
        "invalid choice: 'pull'" under this runtime.

        Resolved against every service in the compose file, not the profile
        selection: `make pull` passes no --profile and the point of the target
        is to warm the whole stack's images, including profile-gated ones.
        Services with a `build:` and no `image:` are skipped, as Compose does.
        """
        defs = self.all_services()
        if services:
            unknown = [s for s in services if s not in defs]
            if unknown:
                die(
                    f"unknown service(s): {', '.join(sorted(unknown))}\n"
                    f"  known: {', '.join(sorted(defs))}"
                )
            wanted = list(services)
        else:
            wanted = sorted(defs)

        # De-duplicated: several services share one image (the three fetchers
        # do not, but stores and extractors can), and pulling the same
        # reference repeatedly is pure wall-clock.
        seen: set[str] = set()
        failures: list[tuple[str, str]] = []
        for service in wanted:
            image = defs[service].get("image")
            if not image:
                print(f"  {service}: no image (build-only), skipping")
                continue
            if image in seen:
                continue
            seen.add(image)
            # A service's own `platform:` wins, exactly as it does for
            # `docker compose pull`; otherwise the host's.
            plat = defs[service].get("platform") or host_platform()
            print(f"  pulling {image} ({plat})")
            if self.dry_run:
                continue
            rc, err = pull_image(
                ["container", "image", "pull", "--platform", plat, image])
            if rc != 0 and _UNSUPPORTED_PLATFORM.search(err):
                # The image does not publish this platform. Most of this
                # stack's own images are amd64-only, so on an arm64 Mac the
                # fast path fails for nine of eleven plugins.
                #
                # Retry letting `container` choose: it picks the only real
                # entry in a single-arch index, which is what
                # `docker compose pull` would have done. Any OTHER failure
                # falls through to the aggregate below instead — retrying it
                # broad would pull every platform in a multi-arch manifest to
                # work around what may have been a momentary network blip.
                print(f"    {plat} not published; retrying with the image's own")
                rc, _err = pull_image(["container", "image", "pull", image])
            if rc != 0:
                failures.append((service, image))

        if failures:
            # Reported together at the end rather than aborting on the first.
            # A partial pull is still useful, and one private image the host
            # has no credentials for should not hide the rest.
            lines = "\n".join(f"  {svc}: {img}" for svc, img in failures)
            die(
                f"{len(failures)} image(s) could not be pulled:\n{lines}\n"
                "  If these are private, log the host in first:\n"
                "      gh auth token | container registry login ghcr.io "
                "-u <user> --password-stdin"
            )

    def exec_(self, service: str, command: list[str], *,
              no_tty: bool = False, user: str | None = None,
              env: list[str] | None = None, workdir: str | None = None) -> int:
        """`docker compose exec <service> <cmd...>`.

        The Makefile reaches every embedded datastore through this and nothing
        else:

            health:            $(COMPOSE) exec valkey valkey-cli ping
                               $(COMPOSE) exec postgres pg_isready -U scraper
                               $(COMPOSE) exec clickhouse clickhouse-client …
            psql:              $(COMPOSE) exec postgres psql -U scraper …
            clickhouse-shell:  $(COMPOSE) exec clickhouse clickhouse-client …
            garage-shell:      $(COMPOSE) exec garage /garage

        Without this subcommand all six died at argparse ("invalid choice:
        'exec'") before the Engine was even built, which made `make health`,
        `make psql`, `make clickhouse-shell` and `make garage-shell` unusable
        on the runtime this script exists to support.

        Resolved against every service in the compose file rather than the
        profile selection, for the reason `_selected()` documents: none of
        those targets pass --profile, and postgres/valkey/clickhouse are all
        profile-gated.
        """
        all_names = self.all_names()
        if service not in all_names:
            die(
                f"unknown service: {service}\n"
                f"  known: {', '.join(sorted(all_names))}"
            )
        if not command:
            die(f"exec {service}: no command given")
        name = all_names[service]
        state = self.state(name)
        if state != "running":
            # "missing" and "stopped" are different operator mistakes and the
            # fix differs, so say which one happened rather than letting
            # `container exec` report its own less specific error.
            hint = ("it has no container — start the stack first"
                    if state == "missing" else f"its container is {state}")
            die(f"cannot exec into {service}: {hint}")

        # -i always: it keeps stdin open, which is what makes
        # `echo 'SELECT 1' | ... exec -T postgres psql` work. `container exec`
        # without it silently discards the piped input and still exits 0 — the
        # query never runs and nothing says so. docker's -T disables the TTY,
        # it does not disconnect stdin.
        argv = ["container", "exec", "-i"]
        # -t only when a terminal is actually on both ends. docker compose
        # allocates one unless -T; under `make health` the command is piped or
        # captured, and asking for a TTY there fails outright.
        if not no_tty and sys.stdin.isatty() and sys.stdout.isatty():
            argv.append("-t")
        if user:
            argv += ["--user", user]
        for pair in env or []:
            argv += ["--env", pair]
        if workdir:
            argv += ["--workdir", workdir]
        argv += [name, *command]
        # Streamed, not captured: `make psql` and `make clickhouse-shell` are
        # interactive shells. The child's exit status is this script's.
        return subprocess.run(argv, check=False).returncode

    def logs(self, follow: bool, services: list[str] | None = None,
             tail: str | None = None) -> None:
        """`services` and `tail` mirror `docker compose logs`, because the
        Makefile passes both:

            logs:     $(COMPOSE) logs -f --tail=100
            logs-%:   $(COMPOSE) logs -f --tail=200 $*

        Without them argparse rejected the flags outright and both targets were
        unusable on this runtime — and even had the flags parsed, this method
        always iterated every container, so `make logs-fetcher-curl` would have
        shown the whole stack.
        """
        # Resolved against every service in the compose file, not the
        # profile-selected subset. `make logs-%` passes no --profile flags, so
        # validating against the selection would reject `make logs-valkey`
        # outright — valkey is gated behind embedded-bus. Reading logs is not
        # a lifecycle operation; if the container exists, show it.
        all_names = self.all_names()
        if services:
            unknown = [svc for svc in services if svc not in all_names]
            if unknown:
                die(
                    f"unknown service(s): {', '.join(sorted(unknown))}\n"
                    f"  known: {', '.join(sorted(all_names))}"
                )
            wanted = {svc: all_names[svc] for svc in services}
        else:
            # Same reasoning as the explicit-name branch above: show the
            # project's containers, not this invocation's profile slice.
            wanted = all_names
        targets = [n for n in wanted.values() if self.state(n) != "missing"]
        if not targets:
            print("no containers from this project are present")
            return

        # `container logs` takes -n/--boot-count, not --tail, and has no
        # line-count option at all. Trim the captured output instead so
        # --tail=N means the same thing under both runtimes.
        def trim(text: str) -> str:
            if tail is None or tail == "all":
                return text
            try:
                n = int(tail)
            except ValueError:
                die(f"--tail expects a number or 'all', got {tail!r}")
            lines = text.splitlines()
            return "\n".join(lines[-n:]) if n > 0 else ""

        if not follow:
            for name in targets:
                print(f"=== {name} ===")
                print(trim(self._capture(["container", "logs", name])))
            return

        # In follow mode the backlog is trimmed once, then the stream continues
        # live — matching `docker compose logs -f --tail=N`.
        for name in targets:
            backlog = trim(self._capture(["container", "logs", name]))
            if backlog:
                for line in backlog.splitlines():
                    sys.stdout.write(f"{name} | {line}\n")

        def stream(name: str) -> None:
            proc = subprocess.Popen(
                ["container", "logs", "--follow", name],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                sys.stdout.write(f"{name} | {line}")

        threads = [
            threading.Thread(target=stream, args=(n,), daemon=True) for n in targets
        ]
        for t in threads:
            t.start()
        try:
            while any(t.is_alive() for t in threads):
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass

    def health(self, name: str) -> None:
        """Backs rt_health in scripts/runtime.sh. container has no health
        primitive, so re-run the declared probe on demand."""
        service = next((s for s, n in self.all_names().items() if n == name), None)
        if service is None:
            print("missing")
            return
        if self.state(name) != "running":
            print("missing" if self.state(name) == "missing" else "starting")
            return
        probe = healthcheck_argv(self.services[service])
        if probe is None:
            print("none")
            return
        rc = subprocess.run(
            ["container", "exec", name, *probe],
            check=False, capture_output=True, text=True,
        ).returncode
        print("healthy" if rc == 0 else "unhealthy")

    def plan(self) -> None:
        print(f"project: {self.project}")
        print(f"network: {self.network}")
        print(f"dns domain: {self.dns_domain or '(none configured)'}")
        print("volumes:")
        for declared in self.volumes:
            print(f"  container volume create {volume_name(self.project, declared)}")
        for index, wave in enumerate(waves(self.services), start=1):
            print(f"wave {index}:")
            for service in wave:
                svc = self.services[service]
                for dep, condition in depends_map(svc, set(self.services)).items():
                    if condition == "service_healthy":
                        probe = healthcheck_argv(self.services[dep])
                        rendered = " ".join(probe) if probe else "(no healthcheck)"
                        print(f"  wait: {container_name(dep, self.services[dep])} "
                              f"-> {rendered}")
                argv = build_run_argv(
                    self.project, service, svc, self.network, self.dns_domain
                )
                print("  " + " ".join(shlex.quote(a) for a in argv))


def parse_cli(argv: list[str]) -> argparse.Namespace:
    """Turn a raw argv into parsed arguments, doing no work.

    Split out of main() so the tests can check that the command lines the
    Makefile renders are actually accepted, without running them. The first
    version of that test executed the rendered `up -d` line — which, once the
    -d bug was fixed, stopped failing at argparse and went on to create a
    network and pull images, so the suite hung and mutated the machine. A
    parser is the right thing to test against arguments.
    """
    parser = argparse.ArgumentParser(
        prog="container-compose",
        description="Run a docker-compose.yml under Apple `container`.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("up", help="create network, volumes, and start services in order")
    down = sub.add_parser("down", help="stop and remove services and the network")
    down.add_argument("-v", "--volumes", action="store_true",
                      help="also delete the project's named volumes")
    restart = sub.add_parser("restart", help="stop then re-run services")
    restart.add_argument("services", nargs="*",
                         help="limit to these services (default: the selection)")
    stop = sub.add_parser("stop", help="stop services without removing them")
    stop.add_argument("services", nargs="*")
    rm = sub.add_parser("rm", help="stop and remove services, keeping volumes")
    rm.add_argument("-f", "--force", action="store_true",
                    help="accepted for `docker compose rm -f` parity; removal is\n"
                         "unconditional either way")
    rm.add_argument("services", nargs="*")
    pull = sub.add_parser("pull", help="fetch each service's image")
    pull.add_argument("services", nargs="*",
                      help="limit to these services (default: all)")
    ex = sub.add_parser("exec", help="run a command in a running service")
    ex.add_argument("-T", dest="no_tty", action="store_true",
                    help="do not allocate a TTY (docker compose parity)")
    ex.add_argument("-u", "--user", default=None)
    ex.add_argument("-e", "--env", action="append", default=None,
                    metavar="KEY=VALUE")
    ex.add_argument("-w", "--workdir", default=None)
    ex.add_argument("service")
    # dest is `argv`, not `command`: add_subparsers already owns `command` as
    # the subcommand name, and a positional of the same name would overwrite
    # it — dispatch would then compare the subcommand against ["psql", ...].
    # REMAINDER, so flags belonging to the inner command survive verbatim:
    # `exec postgres pg_isready -U scraper` must not have -U eaten as ours.
    ex.add_argument("argv", nargs=argparse.REMAINDER)

    ps = sub.add_parser("ps", help="show service status")
    # --status/--services exist for `docker compose ps` parity. The Makefile's
    # _check_embedded runs
    #     $(COMPOSE) ps --status running --services 2>/dev/null | grep -qx <svc>
    # to decide whether a service is embedded or external. argparse rejected
    # both flags, the Makefile discarded the stderr, and the `if` fell through
    # every time — so `make health`, `make psql` and `make clickhouse-shell`
    # reported postgres/valkey/clickhouse as external even while running
    # embedded. A silently wrong answer, which is why it needed a test.
    ps.add_argument("--status", default=None,
                    help="only show services in this state (e.g. running)")
    ps.add_argument("--services", action="store_true",
                    help="print service names only, one per line")
    logs = sub.add_parser("logs", help="show service logs")
    logs.add_argument("-f", "--follow", action="store_true")
    # Both accepted for `docker compose logs` parity — the Makefile passes
    # them, so rejecting them makes `make logs` and `make logs-<svc>` unusable
    # on this runtime.
    logs.add_argument("--tail", default=None,
                      help="number of trailing lines to show, or 'all'")
    logs.add_argument("services", nargs="*", help="limit to these services")
    health = sub.add_parser("health", help="probe one container's declared healthcheck")
    health.add_argument("name")
    sub.add_parser("plan", help="print what `up` would do, without a runtime")
    # `docker compose config --quiet` parity, for the pre-commit hook. Loads
    # and validates the model and prints nothing on success; load_model() dies
    # with a message on anything malformed. Needs no runtime, which is the
    # point — the hook has to work on a machine with no container CLI at all.
    config = sub.add_parser("config", help="validate the compose file and print nothing")
    # Accepted and ignored, for `docker compose config --quiet` parity: the
    # pre-commit hook passes it, and one call site has to serve both runtimes.
    # This subcommand is always quiet on success.
    config.add_argument("-q", "--quiet", action="store_true",
                        help="accepted for docker compose parity; output is silent regardless")


    # --profile is pulled out by hand rather than declared on the parser
    # because `docker compose` accepts it before the subcommand
    # (`docker compose --profile x up`), and argparse subparsers cannot.
    # Accepting it in either position keeps one Makefile call site for both
    # runtimes.
    # Scanning STOPS at `exec`. Everything after it is either exec's own flags
    # or the inner command, which argparse.REMAINDER is supposed to hand
    # through verbatim — and a hand-rolled scan over the whole argv broke that
    # guarantee: a tool inside a container that takes its own `--profile` had
    # the flag silently eaten here and recorded as a compose profile, so the
    # command reaching the container was missing an argument. Profiles are
    # meaningless to exec anyway; it resolves against every service in the file.
    # -f/--file is pulled out the same way, for the same reason: `docker
    # compose -f <file> up` puts it before the subcommand. It has to be known
    # before the model loads, so it is applied to the module-level COMPOSE_FILE
    # here rather than returned.
    #
    # Unlike --profile it is ONLY recognised before the subcommand. Two
    # subcommands have their own -f (`logs -f` is --follow, `rm -f` is
    # --force), and the first version of this scan swallowed both as a compose
    # file path. `docker compose` has the same split: a global -f before the
    # verb, the verb's own -f after it.
    global COMPOSE_FILE
    cleaned: list[str] = []
    seen_command = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "exec":
            cleaned.extend(argv[i:])
            break
        if not seen_command and a in ("-f", "--file"):
            if i + 1 >= len(argv):
                die(f"{a} requires a value")
            COMPOSE_FILE = Path(argv[i + 1])
            i += 2
            continue
        if not seen_command and a.startswith("--file="):
            COMPOSE_FILE = Path(a.split("=", 1)[1])
            i += 1
            continue
        if a == "--profile":
            if i + 1 >= len(argv):
                die("--profile requires a value")
            PROFILE_ARGS.append(argv[i + 1])
            i += 2
            continue
        if a.startswith("--profile="):
            PROFILE_ARGS.append(a.split("=", 1)[1])
            i += 1
            continue
        if not a.startswith("-"):
            seen_command = True
        cleaned.append(a)
        i += 1

    # `up -d` is accepted and ignored: this translator only ever detaches, but
    # the Makefile passes the flag so both runtimes share one call site.
    #
    # Filtered AFTER the --profile extraction above, not before. The Makefile
    # renders `$(COMPOSE) $(COMPOSE_PROFILES) up -d`, so argv starts with
    # --profile and an `argv[:1] == ["up"]` guard never matched — -d survived
    # into the parser and every profile-using target died with
    # `unrecognized arguments: -d`. That is make up, up-direct, up-monitoring,
    # up-monitoring-vm, up-monitoring-ab, up-tunnel and up-garage; only
    # up-external, which passes no profiles, happened to work.
    if cleaned[:1] == ["up"]:
        cleaned = [a for a in cleaned if a not in ("-d", "--detach")]

    return parser.parse_args(cleaned)

def main() -> None:
    # `container` writes its own progress to stderr, which is unbuffered. Our
    # stdout is block-buffered as soon as it is piped (make, CI logs), so
    # without this the two streams interleave out of order and the progress
    # for a container appears before the line announcing it.
    sys.stdout.reconfigure(line_buffering=True)

    args = parse_cli(list(sys.argv[1:]))

    engine = Engine(dry_run=args.command in ("plan", "config"))
    if args.command == "up":
        engine.up()
    elif args.command == "down":
        engine.down(remove_volumes=args.volumes)
    elif args.command == "restart":
        engine.restart(args.services)
    elif args.command == "stop":
        engine.stop(args.services)
    elif args.command == "rm":
        engine.rm(args.services)
    elif args.command == "ps":
        engine.ps(status=args.status, services_only=args.services)
    elif args.command == "logs":
        engine.logs(follow=args.follow, services=args.services, tail=args.tail)
    elif args.command == "pull":
        engine.pull(args.services)
    elif args.command == "exec":
        sys.exit(engine.exec_(
            args.service, args.argv, no_tty=args.no_tty,
            user=args.user, env=args.env, workdir=args.workdir,
        ))
    elif args.command == "health":
        engine.health(args.name)
    elif args.command == "plan":
        engine.plan()
    elif args.command == "config":
        # Engine construction already parsed and validated the whole file.
        pass


if __name__ == "__main__":
    main()
