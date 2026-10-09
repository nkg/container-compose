# CLAUDE.md — container-compose

Single-module Python tool: `container_compose.py` reads a `docker-compose.yml`
and drives Apple's `container` CLI. Packaged with `pyproject.toml`; the console
script is `container-compose`. PyYAML is the only dependency.

- The RUNTIME NOTES block at the top of the module is the design record. Every
  non-obvious branch exists because of a measured `container` behaviour noted
  there. When a `container` release changes something, add a note with the
  version it was measured on before changing the code.
- Tests: `bash tests/test_cli.sh` (or `mise run test`). They run with no
  runtime, through `plan`/`config` and by importing the module. The fixture is
  `tests/fixtures/docker-compose.yml`; extend it rather than adding a second.
- The primary consumer is `HordiaLabs/scraper-deploy` (private), which pins a tag of this
  repo in its `mise.toml` and CI and runs its own integration tests against the
  installed tool. A behaviour change here needs a tag bump there.
- Shell: `shellcheck --severity=warning tests/*.sh` must pass (CI runs it).
