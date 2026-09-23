#!/usr/bin/env bash
# Reusable local/Actions entry points. All services belong to this one run.
set -euo pipefail

: "${CI_WORK_DIR:?Set to a fresh mktemp directory}"
: "${CI_PROJECT:?Set a unique knowledge-v32-review- project name}"
: "${CI_IMAGE:?Set a unique knowledge-system-ci: image tag}"
[[ "$CI_PROJECT" =~ ^knowledge-v32-review-[a-z0-9-]+$ ]]
[[ "$CI_IMAGE" =~ ^knowledge-system-ci:[a-z0-9-]+$ ]]
[[ "$CI_WORK_DIR" = /* && "$CI_WORK_DIR" != / ]]
export DOCKER_HOST=${DOCKER_HOST:-unix:///run/user/1000/docker.sock}
unset DOCKER_CONTEXT
export BUILDX_BUILDER=default
export PIP_CACHE_DIR=${PIP_CACHE_DIR:-$CI_WORK_DIR/pip-cache}
export PIP_CONFIG_FILE=/dev/null PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONNOUSERSITE=1
export OMP_NUM_THREADS=2 MKL_NUM_THREADS=2 TOKENIZERS_PARALLELISM=false
mkdir -p "$CI_WORK_DIR/reports"
reports=$CI_WORK_DIR/reports
python=$CI_WORK_DIR/venv/bin/python
compose=(docker compose --env-file /dev/null --project-name "$CI_PROJECT" -f compose.ci.yml)

case "${1:?Expected preflight/install/quality/unit/services/integration/build/smoke/logs/cleanup}" in
  preflight)
    python3 --version
    python3 -c 'import sys; assert sys.version_info[:2] == (3, 13), "CI host requires Debian Python 3.13"'
    docker version
    docker compose version
    docker buildx version
    security=$(docker info --format '{{json .SecurityOptions}}')
    echo "Docker security options: $security"
    [[ "$security" == *'"name=rootless"'* ]]
    [[ "$DOCKER_HOST" == unix:///run/user/1000/docker.sock ]]
    docker buildx inspect default | tee "$reports/builder.log"
    grep -Eq '^Driver:[[:space:]]+docker$' "$reports/builder.log"
    git cat-file -e 92b25474b94a3d4a369e50363cc3df68a2fba62c:src/knowledge_system/conversation_schema.py
    "${compose[@]}" config --quiet
    ;;
  install)
    # Refuse an existing environment, including one left by an interrupted run.
    [[ ! -e "$CI_WORK_DIR/venv" ]]
    python3 -m venv "$CI_WORK_DIR/venv"
    "$python" --version
    # Match the production image's CPU-only dependency selection.
    "$python" -m pip install --index-url https://download.pytorch.org/whl/cpu torch
    "$python" -m pip install -e '.[dev]'
    "$python" -m pip check
    "$python" -m pip list --format=freeze > "$reports/dependencies.txt"
    ;;
  quality)
    "$python" -m ruff check .
    "$python" -m ruff format --check .
    ;;
  unit)
    "$python" -m pytest tests --ignore=tests/integration -q -ra \
      -o junit_family=xunit2 --junitxml="$reports/unit.xml"
    ;;
  services)
    "${compose[@]}" up -d --wait --wait-timeout 90 postgres
    container_id=$("${compose[@]}" ps -q postgres)
    container=$(docker inspect --format '{{.Name}}' "$container_id")
    container=${container#/}
    address=$("${compose[@]}" port postgres 5432)
    [[ "$address" =~ ^127\.0\.0\.1:[0-9]+$ ]]
    port=${address##*:}
    echo "PostgreSQL healthy on loopback port $port; container=$container"
    for database in knowledge_v311_test knowledge_ci_test knowledge_ci_smoke; do
      "${compose[@]}" exec -T postgres createdb -U knowledge_test "$database"
    done
    # Only generated, shell-quoted values; no production environment is read.
    {
      printf 'export TEST_V32_CONTAINER=%q\n' "$container"
      printf 'export TEST_V32_DATABASE_URL=%q\n' "postgresql://knowledge_test:ci-test-only@127.0.0.1:$port/knowledge_v32_test"
      printf 'export TEST_V311_DATABASE_URL=%q\n' "postgresql://knowledge_test:ci-test-only@127.0.0.1:$port/knowledge_v311_test"
      printf 'export TEST_DATABASE_URL=%q\n' "postgresql://knowledge_test:ci-test-only@127.0.0.1:$port/knowledge_ci_test"
    } > "$CI_WORK_DIR/services.env"
    ;;
  integration)
    # shellcheck source=/dev/null
    source "$CI_WORK_DIR/services.env"
    "$python" -m pytest tests/integration -q -ra \
      -o junit_family=xunit2 --junitxml="$reports/integration.xml"
    "$python" - "$reports/integration.xml" <<'PY'
import sys
import xml.etree.ElementTree as ET

root = ET.parse(sys.argv[1]).getroot()
assert root.findall(".//testcase"), "No integration tests executed"
assert not root.findall(".//skipped"), "Integration tests must not silently skip in CI"
PY
    ;;
  build)
    # The docker driver uses the existing rootless daemon's persistent BuildKit
    # cache and loads output locally. No privileged builder or cache export.
    docker buildx build --builder default --pull --load --progress=plain \
      --tag "$CI_IMAGE" . 2>&1 | tee "$reports/build.log"
    ;;
  smoke)
    "${compose[@]}" run --rm --no-deps -T smoke 2>&1 | tee "$reports/smoke.log"
    ;;
  logs)
    "${compose[@]}" ps --all > "$reports/services.log"
    "${compose[@]}" logs --no-color --timestamps --tail 400 >> "$reports/services.log"
    ;;
  cleanup)
    echo "Cleaning only Compose project $CI_PROJECT and image $CI_IMAGE"
    # Run both cleanups even if the first fails; preserve either failure.
    status=0
    "${compose[@]}" down --volumes --remove-orphans --timeout 15 || status=$?
    image_id=$(docker image ls --quiet "$CI_IMAGE") || status=$?
    if [[ -n "${image_id:-}" ]]; then
      docker image rm "$CI_IMAGE" || status=$?
    fi
    exit "$status"
    ;;
  *) echo "Unknown CI command: $1" >&2; exit 2 ;;
esac
