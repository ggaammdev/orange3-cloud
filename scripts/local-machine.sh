#!/usr/bin/env bash
# Run the cloud machine locally in Docker, with the dev venv's package versions.
#
#   scripts/local-machine.sh up       build the image, start the machine, wait until it answers
#   scripts/local-machine.sh down     stop and remove the machine (keeps the download cache)
#   scripts/local-machine.sh status   show whether it runs, and its versions
#   scripts/local-machine.sh logs     show the machine's output (extra arguments go to
#                                     `docker compose logs`, e.g. -f)
#   scripts/local-machine.sh clean    like down, and also delete the download cache
#
# The token, certificate, key and EnvironmentSpec are written to
# $ORANGE3_CLOUD_LOCAL_DIR (default: ~/.orange3-cloud/local-machine).
#
# Orange's own session (LocalDockerProvider) runs the same image as the container
# orange3-cloud-local, with its secrets in Orange's data folder; both use port 8443,
# so only one of them can run at a time.
#
# The machine stops by itself after $ORANGE3_CLOUD_IDLE_TIMEOUT seconds without
# requests or jobs (default 1800) and after $ORANGE3_CLOUD_MAX_LIFETIME seconds
# (default 43200); 0 turns a limit off. docker-compose.yml passes both on. E.g.:
#   ORANGE3_CLOUD_IDLE_TIMEOUT=20 scripts/local-machine.sh up
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
STATE="${ORANGE3_CLOUD_LOCAL_DIR:-$HOME/.orange3-cloud/local-machine}"
PORT="${ORANGE3_CLOUD_PORT:-8443}"
PYTHON="${PYTHON:-$REPO/.venv/bin/python}"
UP_TIMEOUT="${UP_TIMEOUT:-1800}"
URL="https://localhost:$PORT"

export ORANGE3_CLOUD_LOCAL_DIR="$STATE" ORANGE3_CLOUD_PORT="$PORT"

compose() {
    docker compose -f "$REPO/docker/docker-compose.yml" "$@"
}

# GET $URL$1 with the token, without putting the token on the command line.
api_get() {
    curl -sf --max-time 5 --cacert "$STATE/cert.pem" \
        -H @<(printf 'Authorization: Bearer %s\n' "$(cat "$STATE/token")") "$URL$1"
}

print_info() {
    "$PYTHON" -c '
import json, sys
info = json.load(sys.stdin)
pkgs, failed = info["packages"], info["install_failures"]
server, python, cpus = info["server_version"], info["python_version"], info["cpu_count"]
gib = info["memory_total_bytes"] / 2**30
requested = info.get("python_version_requested")
if requested and requested != python:
    python += f" (requested {requested}, which uv has no build of)"
print(f"  orange3-cloud {server}, Python {python}, {cpus} CPUs, {gib:.1f} GiB")
names = ("orange3", "numpy", "scikit-learn")
print("  " + ", ".join(n + " " + pkgs.get(n, "-") for n in names))
if failed:
    print(f"  {len(failed)} best-effort packages could not be installed: "
          + ", ".join(f["name"] for f in failed))
def span(s):
    if s is None:
        return "never"
    return f"in {s:.0f} s" if s < 120 else f"in {s / 60:.0f} min" if s < 5400 else f"in {s / 3600:.1f} h"
idle, life = info["idle_time_left_seconds"], info["lifetime_left_seconds"]
idle = "after jobs finish or are cancelled" if idle is None and info["idle_timeout_seconds"] else span(idle)
print(f"  Stops by itself: when idle {idle}, at the latest {span(life)}")
'
}

write_config() {
    mkdir -p "$STATE"
    chmod 700 "$STATE"
    (
        umask 077
        openssl rand -hex 32 >"$STATE/token"
        openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 \
            -pkeyopt ec_param_enc:named_curve -nodes \
            -keyout "$STATE/key.pem" -out "$STATE/cert.pem" -days 30 -subj "/CN=localhost" \
            -addext "subjectAltName=DNS:localhost,IP:127.0.0.1" 2>/dev/null
        # Run outside the repository, so a stale .egg-info folder there isn't picked up.
        cd "$STATE"
        "$PYTHON" -c 'from orangecontrib.cloud.env import collect_environment as c
print(c().to_json())' >"$STATE/env-spec.json"
    )
}

cmd_up() {
    [ -x "$PYTHON" ] || { echo "No dev venv at $PYTHON; run scripts/dev-setup.sh first." >&2; exit 1; }
    if [ -n "$(docker ps -q --filter name='^orange3-cloud-local$')" ]; then
        echo "The local machine that Orange started (container orange3-cloud-local) is running" >&2
        echo "on the same port. Stop it from Orange, or: docker rm -f orange3-cloud-local" >&2
        exit 1
    fi
    local start=$SECONDS
    write_config
    echo "Building the image and starting the machine..."
    compose up -d --build --force-recreate --quiet-pull
    echo "Waiting for $URL/v1/info (the first start downloads Python and the packages)..."
    local info
    while ! info="$(api_get /v1/info)"; do
        if [ -z "$(compose ps --status running -q)" ]; then
            echo "The machine stopped while starting:" >&2
            compose logs --tail 60 >&2
            exit 1
        fi
        if (( SECONDS - start > UP_TIMEOUT )); then
            echo "Timed out after ${UP_TIMEOUT}s. Last output:" >&2
            compose logs --tail 60 >&2
            exit 1
        fi
        sleep 2
    done
    echo "The machine is up after $((SECONDS - start)) s:"
    printf '%s\n' "$info" | print_info
    echo "  URL:         $URL"
    echo "  Token:       $STATE/token"
    echo "  Certificate: $STATE/cert.pem"
}

cmd_status() {
    compose ps
    local info
    if [ -f "$STATE/token" ] && info="$(api_get /v1/info)"; then
        echo "$URL answers:"
        printf '%s\n' "$info" | print_info
    else
        echo "$URL doesn't answer."
    fi
}

case "${1:-}" in
    up) cmd_up ;;
    down) compose down ;;
    status) cmd_status ;;
    logs) shift; compose logs "$@" ;;
    clean) compose down --volumes ;;
    *)
        echo "usage: $0 up|down|status|logs|clean" >&2
        exit 2
        ;;
esac
