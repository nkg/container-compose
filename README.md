# container-compose

A minimal Compose implementation for Apple [`container`](https://github.com/apple/container).
It reads an ordinary `docker-compose.yml` and drives the `container` CLI, so one
compose file can run under Docker on Linux and under `container` on macOS with
no second stack definition to keep in sync.

Apple ships no compose support, and the community plugins do not implement
healthcheck-gated `depends_on`, which any stack whose services connect to each
other at startup needs.

## Install

```bash
uv tool install git+https://github.com/HordiaLabs/container-compose.git@v0.1.0
# or
pip install git+https://github.com/HordiaLabs/container-compose.git@v0.1.0
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

## Development

```bash
mise install && mise run setup   # editable install into .venv
mise run test                    # tests/test_cli.sh against tests/fixtures/
```

Tests need no runtime. The reference stack this grew up against is
[HordiaLabs/scraper-deploy](https://github.com/HordiaLabs/scraper-deploy),
whose own `tests/test_runtime.sh` runs the installed tool against the real
compose file and diffs its interpolation against `docker compose config`.
