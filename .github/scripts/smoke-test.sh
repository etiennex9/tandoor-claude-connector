#!/usr/bin/env bash
# Starts the gateway image the ways it's deployed, and fails when it doesn't start (or stop) as gateway/entrypoint.sh
# says:
#   1. As apps (568:568), as deploy/truenas.yaml runs it, with a /data that user owns: it serves /healthz as 568.
#   2. As 568:568 with a root-owned /data, like a new TrueNAS dataset nobody gave to apps: it exits 1 naming /data,
#      before Python fails on it with a traceback.
#   3. As the image's own user, node (1000:1000), with a new named volume, as docker compose runs it: it serves
#      /healthz as 1000.
#
# The gateway reads its OIDC discovery document on start, so mock_oidc.py serves one on a network of its own. Tandoor
# isn't needed until the first MCP request.
#
# Usage: smoke-test.sh <image>, e.g. smoke-test.sh tandoor-claude-gateway:smoke
set -euo pipefail

image=${1:?usage: smoke-test.sh <image>}
here=$(dirname "$0")
name=gateway-smoke-$$
gateway_env=(
    -e PUBLIC_BASE_URL=https://recipe.example.com/claude
    -e OIDC_CONFIG_URL=http://oidc:8080/.well-known/openid-configuration
    -e OIDC_CLIENT_ID=smoke-test
    -e OIDC_CLIENT_SECRET=smoke-test
    -e JWT_SIGNING_KEY=00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff
    -e TANDOOR_URL=http://127.0.0.1:9
    -e TANDOOR_TOKEN=x
)

cleanup() {
    docker rm -f "$name-oidc" "$name-apps" "$name-root-data" "$name-node" >/dev/null 2>&1 || true
    docker volume rm "$name" >/dev/null 2>&1 || true
    docker network rm "$name" >/dev/null 2>&1 || true
}
trap cleanup EXIT

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

# Prints a container's output, indented.
show_logs() {
    echo "   Gateway output:"
    docker logs "$1" 2>&1 | sed 's/^/    /'
}

# Retries "$@" once a second until it succeeds, for up to $tries seconds, or until container $1 stops.
retry_while_running() {
    local container=$1 tries=$2
    shift 2
    until "$@"; do
        tries=$((tries - 1))
        [ "$tries" -gt 0 ] && [ "$(docker inspect -f '{{.State.Running}}' "$container")" = true ] || return 1
        sleep 1
    done
}

# Prints the body of URL $2, fetched from inside container $1.
get() {
    docker exec "$1" python3 -c 'import sys, urllib.request
print(urllib.request.urlopen(sys.argv[1], timeout=2).read().decode())' "$2" 2>/dev/null
}

healthz_ok() {
    [ "$(get "$1" http://127.0.0.1:8000/healthz)" = ok ]
}

# Starts the gateway as container $name-$1 with the docker run options that follow $2, and checks that it serves
# /healthz, that PID 1 runs as uid:gid $2, and that it created its OAuth store in /data as that user.
expect_healthy() {
    local container=$name-$1 ids=$2 healthy=true actual
    shift 2
    docker run -d --name "$container" --network "$name" "$@" "${gateway_env[@]}" "$image" >/dev/null
    retry_while_running "$container" 60 healthz_ok "$container" || healthy=false
    show_logs "$container"
    $healthy || fail "the gateway didn't answer /healthz with ok"
    actual=$(docker exec "$container" cat /proc/1/status |
        awk '/^(Uid|Gid):/ { $1 = $1; printf "%s%s", sep, $0; sep = ", " }')
    local uid=${ids%:*} gid=${ids#*:}
    [ "$actual" = "Uid: $uid $uid $uid $uid, Gid: $gid $gid $gid $gid" ] ||
        fail "PID 1 should run as $ids, but has $actual"
    actual=$(docker exec "$container" stat -c %u:%g /data/oauth-proxy) ||
        fail "the gateway didn't create /data/oauth-proxy"
    [ "$actual" = "$ids" ] || fail "/data/oauth-proxy is owned by $actual, not $ids"
    echo "   OK"
}

docker network create "$name" >/dev/null
docker run -d -q --name "$name-oidc" --network "$name" --network-alias oidc python:3.13-alpine \
    python -c "$(<"$here/mock_oidc.py")" >/dev/null
retry_while_running "$name-oidc" 30 get "$name-oidc" http://127.0.0.1:8080/.well-known/openid-configuration \
    >/dev/null || fail "the mock OIDC server didn't start: $(docker logs "$name-oidc" 2>&1)"

echo "1. As apps (568:568) with a /data it owns, as on TrueNAS: should serve /healthz as 568"
expect_healthy apps 568:568 --user 568:568 --tmpfs /data:uid=568,gid=568,mode=700

echo "2. As 568:568 with a root-owned /data: should exit 1 with the entrypoint's message"
docker run -d --name "$name-root-data" --network "$name" --user 568:568 --tmpfs /data:uid=0,gid=0,mode=755 \
    "${gateway_env[@]}" "$image" >/dev/null
status=$(timeout 60 docker wait "$name-root-data") || fail "the gateway was still running after 60 seconds"
show_logs "$name-root-data"
logs=$(docker logs "$name-root-data" 2>&1)
[ "$status" = 1 ] || fail "the gateway exited with $status, not 1"
grep -qF "uid 568 can't use /data." <<<"$logs" || fail "the entrypoint didn't say which path it can't use"
if grep -qF Traceback <<<"$logs"; then
    fail "the gateway failed with a Python traceback instead of the entrypoint's message"
fi
echo "   OK"

echo "3. As the image's user, node (1000:1000), with a new named volume, as with compose: should serve /healthz as 1000"
docker volume create "$name" >/dev/null
expect_healthy node 1000:1000 -v "$name:/data"
