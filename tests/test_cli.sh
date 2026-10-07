#!/usr/bin/env bash
# Tests for container-compose that need no runtime: everything goes through
# `plan` / `config`, or imports the module and calls pure functions.
#
# The fixture in tests/fixtures/ is the stack under test. The reference stack
# that drove most of these behaviours is HordiaLabs/scraper-deploy, whose own
# tests/test_runtime.sh exercises the installed tool against the real file.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
FIXTURE="$SCRIPT_DIR/fixtures/docker-compose.yml"
MOD="$REPO_DIR/container_compose.py"
PASS=0
FAIL=0

red()   { printf '\033[0;31mFAIL: %s\033[0m\n' "$*"; }
green() { printf '\033[0;32mPASS: %s\033[0m\n' "$*"; }
ok() { green "$1"; PASS=$((PASS + 1)); }
no() { red "$1"; FAIL=$((FAIL + 1)); }

# Prefer the installed console script, so the packaging is what gets tested;
# fall back to the module for a bare checkout.
if command -v container-compose >/dev/null 2>&1; then
  CC=(container-compose)
else
  CC=(python3 "$MOD")
fi

if ! python3 -c "import yaml" 2>/dev/null; then
  red "PyYAML is not installed. Run: pip install . (or uv pip install -e .)"
  exit 1
fi

echo "Running container-compose tests (${CC[*]})..."
echo

# ── Basics ──────────────────────────────────────────────────────────────────

python3 -c "import ast; ast.parse(open('$MOD').read())" 2>/dev/null \
  && ok "module parses" || no "module parses"

"${CC[@]}" -f "$FIXTURE" config --quiet >/dev/null 2>&1 \
  && ok "config --quiet accepts the fixture" || no "config --quiet rejects the fixture"

out="$("${CC[@]}" -f "$FIXTURE" --profile embedded-db --profile embedded-bus plan 2>&1)"
[[ "$out" == *"project: fixture"* ]] && ok "plan reads the project name" || no "plan project name: $out"

# ── Compose-file resolution ─────────────────────────────────────────────────

cd "$SCRIPT_DIR/fixtures" || exit 1
"${CC[@]}" config --quiet >/dev/null 2>&1 \
  && ok "no -f: resolves docker-compose.yml in the current directory" \
  || no "no -f: did not find docker-compose.yml in cwd"
cd "$REPO_DIR" || exit 1

out="$("${CC[@]}" config 2>&1 || true)"
[[ "$out" == *"compose file not found"* ]] \
  && ok "no -f and no file in cwd: clear error" || no "missing file error unclear: $out"

RT_COMPOSE_FILE="$FIXTURE" "${CC[@]}" config --quiet >/dev/null 2>&1 \
  && ok "RT_COMPOSE_FILE selects the file" || no "RT_COMPOSE_FILE ignored"
COMPOSE_FILE="$FIXTURE" "${CC[@]}" config --quiet >/dev/null 2>&1 \
  && ok "COMPOSE_FILE selects the file (docker compose parity)" || no "COMPOSE_FILE ignored"

"${CC[@]}" --file="$FIXTURE" config --quiet >/dev/null 2>&1 \
  && ok "--file=<path> form" || no "--file=<path> form rejected"

out="$("${CC[@]}" -f 2>&1 || true)"
[[ "$out" == *"requires a value"* ]] && ok "-f without a value is an error" || no "-f without a value: $out"

# ── Profiles ────────────────────────────────────────────────────────────────

plan_names() { "${CC[@]}" -f "$FIXTURE" "$@" plan 2>/dev/null | grep -oE '\-\-name [a-z0-9_-]+' | awk '{print $2}' | sort | tr '\n' ' '; }

none="$(plan_names)"
[[ "$none" == *"app"* && "$none" != *"bus"* && "$none" != *"db"* ]] \
  && ok "no profile: only unprofiled services selected" || no "no-profile selection: $none"

both="$(plan_names --profile embedded-db --profile embedded-bus)"
[[ "$both" == *"bus"* && "$both" == *"db"* ]] \
  && ok "--profile selects profiled services" || no "profile selection: $both"

env_sel="$(COMPOSE_PROFILES=embedded-db plan_names)"
[[ "$env_sel" == *"db"* && "$env_sel" != *"bus"* ]] \
  && ok "COMPOSE_PROFILES env selects profiles" || no "COMPOSE_PROFILES: $env_sel"

# ── Refusals ────────────────────────────────────────────────────────────────

out="$("${CC[@]}" -f "$FIXTURE" --profile monitoring plan 2>&1 || true)"
[[ "$out" == *"cadvisor"* && "$out" == *"cannot run under Apple"* ]] \
  && ok "docker-socket / privileged services are refused with a reason" || no "refusal: $out"

# ── Interpolation (pure functions) ──────────────────────────────────────────

py() { python3 - "$MOD"; }

nested="$(py <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("cc", sys.argv[1])
cc = importlib.util.module_from_spec(spec); spec.loader.exec_module(cc)
print(cc.interpolate("${DATABASE_URL:-postgres://app:${POSTGRES_PASSWORD}@db:5432/app}", {"POSTGRES_PASSWORD": "p@ss"}, "t"))
PY
)"
[[ "$nested" == "postgres://app:p@ss@db:5432/app" ]] \
  && ok "nested \${VAR:-...\${VAR2}...} interpolates" || no "nested interpolation: $nested"

single_dash="$(py <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("cc", sys.argv[1])
cc = importlib.util.module_from_spec(spec); spec.loader.exec_module(cc)
print(repr(cc.interpolate("${PROXY_URL-http://router:8888}", {"PROXY_URL": ""}, "t")))
PY
)"
[[ "$single_dash" == "''" ]] \
  && ok "\${VAR-default} keeps an explicit empty value" || no "\${VAR-default}: $single_dash"

# ── Model: required:false deps, x-container, environment list form ─────────

if (cd "$SCRIPT_DIR/fixtures" && py <<'PY' >/dev/null 2>&1
import importlib.util, sys
spec = importlib.util.spec_from_file_location("cc", sys.argv[1])
cc = importlib.util.module_from_spec(spec); sys.modules["cc"] = cc; spec.loader.exec_module(cc)
_, services, _, _ = cc.load_model()
present = set(services)
assert "app" in present and "bus" not in present and "db" not in present, present
for name, svc in services.items():
    for dep in cc.depends_map(svc, present):
        assert dep in present, f"dangling {name}->{dep}"
PY
); then ok "profile-excluded required:false deps are dropped (no KeyError in up)"; else no "required:false deps dangle"; fi

if (cd "$SCRIPT_DIR/fixtures" && COMPOSE_PROFILES=embedded-db POSTGRES_PASSWORD=x py <<'PY' >/dev/null 2>&1
import importlib.util, sys
spec = importlib.util.spec_from_file_location("cc", sys.argv[1])
cc = importlib.util.module_from_spec(spec); sys.modules["cc"] = cc; spec.loader.exec_module(cc)
_, services, _, _ = cc.load_model()
env = services["db"]["environment"]
assert env["PGDATA"] == "/var/lib/postgresql/data/pgdata", env     # override applied
assert env["POSTGRES_PASSWORD"] == "x", env                          # base keys survive a mapping merge
assert "x-container" not in services["db"]
PY
); then ok "x-container merges environment key by key"; else no "x-container merge"; fi

if HOST_PASSTHROUGH=from-host py <<'PY' >/dev/null 2>&1
import importlib.util, os, sys
spec = importlib.util.spec_from_file_location("cc", sys.argv[1])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
os.environ.pop("UNSET_ON_HOST", None)
argv = m.build_run_argv("proj", "svc", {"image": "x", "environment":
    ["A=1", "B=two=three", "HOST_PASSTHROUGH", "UNSET_ON_HOST"]}, "net", None)
envs = [argv[i+1] for i, a in enumerate(argv) if a == "--env"]
assert "A=1" in envs and "B=two=three" in envs and "HOST_PASSTHROUGH=from-host" in envs, envs
assert not any(e.startswith("UNSET_ON_HOST") for e in envs), envs
PY
then ok "environment list form: bare VAR inherits, unset is dropped, '=' in values survives"
else no "environment list form"; fi

# ── CLI parsing ─────────────────────────────────────────────────────────────

if py <<'PY' >/dev/null 2>&1
import importlib.util, sys
spec = importlib.util.spec_from_file_location("cc", sys.argv[1])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
# --profile before the subcommand, -d ignored on up, and exec's REMAINDER kept whole
m.PROFILE_ARGS.clear()
a = m.parse_cli(["--profile", "x", "up", "-d"]); assert a.command == "up" and m.PROFILE_ARGS == ["x"]
m.PROFILE_ARGS.clear()
a = m.parse_cli(["exec", "db", "psql", "--profile", "inner"]); assert a.argv == ["psql", "--profile", "inner"] and m.PROFILE_ARGS == []
a = m.parse_cli(["ps", "--status", "running", "--services"]); assert a.status == "running" and a.services
a = m.parse_cli(["logs", "--tail=5", "-f", "db"]); assert a.tail == "5" and a.follow and a.services == ["db"], a
a = m.parse_cli(["rm", "-f", "db"]); assert a.force and a.services == ["db"], a      # rm's own -f, not ours
a = m.parse_cli(["-f", "/x/compose.yml", "plan"]); assert str(m.COMPOSE_FILE) == "/x/compose.yml"
for verb in ("up", "down", "restart", "stop", "rm", "pull", "ps", "logs", "plan", "config"):
    assert m.parse_cli([verb]).command == verb
PY
then ok "parse_cli: --profile/-f before the verb, logs -f / rm -f untouched, -d ignored, exec REMAINDER intact"
else no "parse_cli"; fi

out="$("${CC[@]}" -f "$FIXTURE" up --definitely-not-a-flag 2>&1 || true)"
[[ "$out" == *"unrecognized arguments"* ]] && ok "undeclared flags are still rejected" || no "undeclared flag accepted: $out"

# ── Rendered run lines ──────────────────────────────────────────────────────

plan="$("${CC[@]}" -f "$FIXTURE" --profile embedded-db plan 2>&1)"
[[ "$plan" == *"--mount type=tmpfs"*"2g"* || "$plan" == *"2147483648"* ]] \
  && ok "shm_size renders as a sized tmpfs mount" || no "shm_size not translated"
[[ "$plan" == *"--platform linux/amd64"* ]] \
  && ok "a declared platform reaches the run line" || no "platform dropped"
[[ "$plan" == *"PGDATA=/var/lib/postgresql/data/pgdata"* ]] \
  && ok "x-container environment reaches the run line" || no "x-container env dropped"
[[ "$plan" != *"-p 127.0.0.1:4000"* && "$plan" != *"--publish"* ]] \
  && ok "ports: are not published (default-network-only in container)" || no "ports published"

echo
echo "================================"
echo "Results: $PASS passed, $FAIL failed"
echo "================================"
[[ $FAIL -gt 0 ]] && exit 1 || exit 0
