# container-compose

[![CI](https://github.com/nkg/container-compose/actions/workflows/ci.yml/badge.svg)](https://github.com/nkg/container-compose/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

A minimal Compose implementation for Apple [`container`](https://github.com/apple/container).
It reads an ordinary `docker-compose.yml` and drives the `container` CLI, so one
compose file can run under Docker on Linux and under `container` on macOS with
no second stack definition to keep in sync.

Apple ships no compose support. Several community tools fill the gap; this
one exists because a 13-service stack whose services connect to each other at
startup needs healthcheck-gated `depends_on`, profiles and exact `docker
compose config` interpolation, and none of the others had all three when it
was written. See [How it compares](#how-it-compares) before choosing.

## Install

```bash
uv tool install container-compose
# or
pip install container-compose
# or, straight from a tag
uv tool install git+https://github.com/nkg/container-compose.git@v0.1.2
```

PyYAML is the only dependency. Python 3.10 or newer.

## Use

```bash
container-compose -f docker-compose.yml --profile embedded-db up -d
container-compose ps --status running --services
container-compose logs -f app
container-compose exec db psql -U app
container-compose down -v
container-compose plan      # print what `up` would run, no runtime needed
container-compose config -q # validate the file, print nothing (pre-commit hook)
```

The compose file is resolved the way `docker compose` does it: `-f`, then
`$COMPOSE_FILE`, then `compose.yaml` / `compose.yml` / `docker-compose.yaml` /
`docker-compose.yml` in the current directory. `$RT_COMPOSE_FILE` is also
honoured, for callers that already use that name to point both runtimes at
another stack. `.env` is read from the compose file's directory.

Profiles come from `--profile` (before or after the verb) or
`$COMPOSE_PROFILES`.

## What is translated

Only the subset of Compose that real stacks have needed so far:

- `profiles`, `depends_on` with `condition: service_healthy` and
  `required: false`, healthchecks in both `CMD` and `CMD-SHELL` form
- `${VAR}`, `${VAR:-default}`, `${VAR-default}` and nested defaults, matching
  `docker compose config` byte for byte
- `environment` in mapping and list form (a bare `VAR` inherits from the host)
- named volumes, bind mounts, `shm_size` (as a sized tmpfs mount, since
  `--tmpfs /dev/shm:size=` is not honoured), `command` in both forms,
  `deploy.resources.limits`, `platform`
- `x-container:` blocks, merged over the service for `container`-only
  overrides (Compose ignores `x-*`, so `docker compose config` stays valid)

Two documented gaps:

- `ports:` is not published. On `container`, publishing only works on the
  `default` network; services on a user-defined network are reached by IP, or
  by name through a DNS domain (`container system property set dns.domain`).
- Services that bind-mount the Docker socket or need `--privileged` (cadvisor,
  promtail) are refused with an explanation: they read Docker's own API and
  nothing here could give them one.

Every behaviour was measured against a real `container` release (0.12.1 and
1.4.1 so far). The RUNTIME NOTES at the top of `container_compose.py` record
what was observed and which decision each observation forced.

## How it compares

Checked against each project's README in October 2026; they move quickly, so
re-check before deciding.

| | This tool | [Mcrich23/Container-Compose](https://github.com/Mcrich23/Container-Compose) | [docker-for-apple-container](https://github.com/appautomaton/docker-for-apple-container) |
|---|---|---|---|
| Language, install | Python, `uv tool install` / `pip` | Swift, Homebrew | Shell wrapper, Homebrew |
| Shape | `container-compose` CLI | `container-compose` CLI | A `docker` shim, for tools that expect that binary |
| `depends_on` with `condition: service_healthy` | Yes, gates `up` on the healthcheck | Startup order documented; conditions not | Not documented |
| `profiles`, `COMPOSE_PROFILES` | Yes | Not documented | Not documented |
| `${VAR:-default}` interpolation | Byte-for-byte with `docker compose config` (tested) | `.env` documented; substitution not | Not documented |
| `shm_size`, `deploy.resources.limits`, `x-container:` overrides | Yes | Not documented | Not documented |
| `ports:` | Not published (documented gap, see above) | Volume and network mapping documented | Documented |
| Apple `container` versions measured | 0.12.1, 1.4.1 | Recommends macOS 26 for DNS | 1.4.1 only, stated |

Pick the Swift tool if you want Homebrew and your compose file is simple. Pick
the `docker` shim if an IDE or script insists on calling `docker`. Pick this
one if your stack's startup depends on healthchecks or profiles, or you need
the `up` plan to be inspectable (`container-compose plan`) and testable
without a runtime.

[noghartt/container-compose](https://github.com/noghartt/container-compose)
is archived by its author and points at the Swift tool.

## Development

```bash
mise install && mise run setup   # editable install into .venv
mise run test                    # tests/test_cli.sh against tests/fixtures/
```

Tests need no runtime. The tool grew up against a private 13-service
deployment repo, whose own integration suite runs the installed tool against
its real compose file and diffs the interpolation against `docker compose
config`; that is where the measured behaviours above came from.

## License

[MIT](LICENSE).
