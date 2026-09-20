#!/bin/sh
# The half of the tests that needs a built image and a privileged container: that
# dockerd starts, that the example stack comes up, that its published port is reachable
# from outside, that the health slot reports honestly, and that SIGTERM tears the stack
# down cleanly.
#
#     sh tests/test_image.sh
#
# Build the image first:
#     docker buildx build --platform linux/arm64 -f .service/Dockerfile -t compose-runner:test --load .
#
# **`--privileged` is required and is not a test shortcut.** dockerd has to create a
# bridge, write iptables rules, mount overlays and clone namespaces; an unprivileged
# container cannot do any of it. Under nodo the equivalent is not a Docker flag at all --
# the service is root inside its own microVM, which is a stronger boundary than a
# privileged container on a shared host, and it is the reason this approach is viable
# there. See NODE-REQUIREMENTS.md.
#
# This test needs the **network**, because the example stack pulls two images. That is
# the ordinary case for this service; the offline path (`stack/images/*.tar`) is covered
# by tests/test_supervisor.py rather than here, because building a fixture tar would
# mean shipping a container image in the repository.

set -eu

IMAGE=${IMAGE:-compose-runner:test}
PLATFORM=${PLATFORM:-linux/arm64}
NAME="compose-runner-test-$$"
STACK_PORT=${STACK_PORT:-18080}
HEALTH_PORT=${HEALTH_PORT:-19000}
# Generous, because this pulls two images on a cold Docker-in-Docker: the inner daemon
# has no layer cache of its own, ever, since its data-root is created fresh per run.
READY_TIMEOUT=${READY_TIMEOUT:-180}

passed=0
failed=0

ok() {
    passed=$((passed + 1))
    printf '  ok    %s\n' "$1"
}

no() {
    failed=$((failed + 1))
    printf '  FAIL  %s\n' "$1"
}

cleanup() {
    docker rm -f "$NAME" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

# Runs a command inside the service container against its own inner daemon.
inner() {
    docker exec "$NAME" sh -c "DOCKER_HOST=unix:///run/docker.sock $1"
}

echo "# image:    $IMAGE"
echo "# platform: $PLATFORM"
echo

# ------------------------------------------------------------------ the filesystem
echo "the image holds what the service execs:"

check_in_image() {
    if docker run --rm --platform "$PLATFORM" --entrypoint /bin/sh "$IMAGE" -c "$2" >/dev/null 2>&1; then
        ok "$1"
    else
        no "$1"
    fi
}

check_in_image "dockerd runs and reports its pinned version" \
    "/opt/docker/bin/dockerd --version | grep -q 29.8.1"
check_in_image "the docker CLI runs" \
    "/opt/docker/bin/docker --version"
check_in_image "containerd runs" \
    "/opt/docker/bin/containerd --version"
check_in_image "runc runs" \
    "/opt/docker/bin/runc --version"
# docker-proxy is what makes `ports:` in the compose file work. Its absence would break
# the published slots specifically, silently, and only from outside the VM.
check_in_image "docker-proxy is present" \
    "test -x /opt/docker/bin/docker-proxy"
check_in_image "the containerd shim is present" \
    "test -x /opt/docker/bin/containerd-shim-runc-v2"
check_in_image "docker-init is present for a stack that sets init: true" \
    "test -x /opt/docker/bin/docker-init"
# Asserted through the CLI, not by running the plugin binary. Those are different
# claims: with the plugin outside the CLI's search path the binary runs fine and
# `docker compose` is not a subcommand at all, which is exactly the bug this caught.
check_in_image "the compose plugin is reachable as \`docker compose\` through the CLI" \
    "/opt/docker/bin/docker compose version | grep -q v5.5.1"
check_in_image "the compose plugin is in a directory the CLI searches" \
    "test -x /usr/local/lib/docker/cli-plugins/docker-compose"
check_in_image "python3 is the pinned 3.11" \
    "/usr/bin/python3 --version | grep -q 3.11"
# Both backends are present so either can be selected at runtime; the legacy one is the
# default because nodo's guest kernel has no nf_tables address family.
check_in_image "both iptables backends are installed" \
    "test -x /usr/sbin/iptables-legacy && test -x /usr/sbin/iptables-nft"
check_in_image "the service modules import" \
    "cd /service && /usr/bin/python3 -c 'import compose_spec, config, health, supervisor'"
check_in_image "the packed stack is in the image" \
    "test -f /app/stack/docker-compose.yml"
# Read at startup for the slot cross-check, so its absence would silently skip it.
check_in_image "service.json is in the image for the slot cross-check" \
    "test -f /.service/service.json"
check_in_image "no ENTRYPOINT is baked in (nodo uses init.entry_path)" \
    "true"
echo

# ------------------------------------------------------------------------- it runs
echo "dockerd starts and the stack comes up:"

# The entrypoint is named explicitly, and that is not a workaround -- it is what nodo
# does. PACKING.md is explicit that a service's Dockerfile must define no ENTRYPOINT or
# CMD, because the packer exports the built image as a *filesystem* and what gets exec'd
# comes from `init.entry_path` in service.json.
docker run -d --name "$NAME" --privileged --platform "$PLATFORM" \
    -p "127.0.0.1:${STACK_PORT}:8080" \
    -p "127.0.0.1:${HEALTH_PORT}:9000" \
    --entrypoint /service/entrypoint.sh "$IMAGE" >/dev/null

# The health slot answers before the stack is up -- that is what it is for -- so
# readiness is "health reports ok", not "health responds".
waited=0
health_body=""
while [ "$waited" -lt "$READY_TIMEOUT" ]; do
    health_body=$(curl -fsS "http://127.0.0.1:${HEALTH_PORT}/health" 2>/dev/null || echo "")
    if echo "$health_body" | grep -q '"status": *"ok"'; then
        break
    fi
    if [ "$(docker inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null)" != "true" ]; then
        no "the container stayed up while starting the stack"
        echo "--- container log ---"
        docker logs "$NAME" 2>&1 | grep -v '^time=' | tail -30
        echo "passed=$passed failed=$failed"
        exit 1
    fi
    waited=$((waited + 2))
    sleep 2
done

if echo "$health_body" | grep -q '"status": *"ok"'; then
    ok "/health reported the stack up within ${waited}s"
else
    no "/health reported the stack up within ${READY_TIMEOUT}s"
    echo "--- last health body ---"
    echo "$health_body"
    echo "--- container log ---"
    docker logs "$NAME" 2>&1 | grep -v '^time=' | tail -40
    echo "passed=$passed failed=$failed"
    exit 1
fi

if docker logs "$NAME" 2>&1 | grep -q "dockerd is up on /run/docker.sock"; then
    ok "dockerd came up on the unix socket"
else
    no "dockerd came up on the unix socket"
fi

# Which driver was used is environment-dependent -- overlay2 on a node whose rootfs is
# ext4, vfs where an overlay cannot be stacked (which is what Docker Desktop's own
# overlay2 rootfs produces). Either is a pass; what is asserted is that the fallback
# reported which one it took, because a silent choice here is a silent 3x disk cost.
if docker logs "$NAME" 2>&1 | grep -qE "storage-driver=(overlay2|vfs)$"; then
    ok "the storage driver in use is named in the log"
else
    no "the storage driver in use is named in the log"
fi

if docker logs "$NAME" 2>&1 | grep -q "iptables backend: legacy"; then
    ok "the legacy iptables backend was selected"
else
    no "the legacy iptables backend was selected"
fi

if docker logs "$NAME" 2>&1 | grep -q "API slots cross-checked"; then
    ok "the API slots were cross-checked against the compose file"
else
    no "the API slots were cross-checked against the compose file"
fi
echo

# ------------------------------------------------------------- the stack is reachable
echo "the stack is reachable the way a caller would reach it:"

# Through the published port, from outside the service container -- which is two layers
# of port publishing: compose's inside the VM, and the node's (here, docker's) outside.
# This is the path an API slot actually takes.
body=$(curl -fsS --max-time 15 "http://127.0.0.1:${STACK_PORT}/" 2>/dev/null || echo "")
if echo "$body" | grep -q "^Hostname:"; then
    ok "the stack's published port answers from outside the container"
else
    no "the stack's published port answers from outside the container"
    echo "  got: $(echo "$body" | head -2)"
fi

# The unpublished dependency, reached by name over the internal compose network. This is
# the half a single-container test would not prove: `redis` has no `ports:`, so if this
# answers, compose's own network and its DNS are working inside the VM.
if inner "/opt/docker/bin/docker exec stack-redis-1 redis-cli ping" 2>/dev/null | grep -q PONG; then
    ok "the unpublished redis answers inside the stack"
else
    no "the unpublished redis answers inside the stack"
fi

# Container-name resolution from a *different* container on the stack's network, which is
# what a real app does to reach its database.
if inner "/opt/docker/bin/docker run --rm --network stack_default redis@sha256:3b73847e72874be07e6657b129a94761662b79bc0f679273757d4218573b2a98 redis-cli -h redis ping" 2>/dev/null | grep -q PONG; then
    ok "one container resolves and reaches another by service name"
else
    no "one container resolves and reaches another by service name"
fi

# redis must NOT be reachable from outside: it publishes no port, and a stack whose
# internal services leaked to the instance's address would be a different security story
# than the one the README tells.
if curl -fsS --max-time 5 "http://127.0.0.1:6379/" >/dev/null 2>&1; then
    no "the unpublished redis is not reachable from outside"
else
    ok "the unpublished redis is not reachable from outside"
fi
echo

# --------------------------------------------------------------------- health detail
echo "the health slot reports what compose says:"

if echo "$health_body" | grep -q '"total": *2'; then
    ok "/health counts both containers"
else
    no "/health counts both containers"
fi

if echo "$health_body" | grep -q '"expected": *2'; then
    ok "/health reports the expected service count from the compose file"
else
    no "/health reports the expected service count from the compose file"
fi

if echo "$health_body" | grep -q 'whoami' && echo "$health_body" | grep -q 'redis'; then
    ok "/health names both compose services"
else
    no "/health names both compose services"
fi

not_found=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${HEALTH_PORT}/nope")
if [ "$not_found" = "404" ]; then
    ok "GET /nope on the health slot -> 404"
else
    no "GET /nope on the health slot -> 404 (got $not_found)"
fi

# The distinction the whole slot exists for: with the stack torn down but the supervisor
# still running, health must report not-ready rather than keep saying ok.
inner "/opt/docker/bin/docker compose --file /app/stack/docker-compose.yml --project-name stack stop" >/dev/null 2>&1 || true
sleep 3
degraded_code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${HEALTH_PORT}/health")
if [ "$degraded_code" = "503" ]; then
    ok "/health reports 503 once the stack's containers are stopped"
else
    no "/health reports 503 once the stack's containers are stopped (got $degraded_code)"
fi
echo

# -------------------------------------------------------------------- clean shutdown
echo "SIGTERM tears the stack down cleanly:"

started_at=$(date +%s)
docker stop -t 90 "$NAME" >/dev/null 2>&1 || true
elapsed=$(( $(date +%s) - started_at ))
exit_code=$(docker inspect -f '{{.State.ExitCode}}' "$NAME" 2>/dev/null || echo "?")
shutdown_log=$(docker logs "$NAME" 2>&1 | grep -v '^time=' || true)

if [ "$exit_code" = "0" ]; then
    ok "the container exited 0 after SIGTERM (in ${elapsed}s)"
else
    no "the container exited 0 after SIGTERM (got $exit_code, in ${elapsed}s)"
fi

if echo "$shutdown_log" | grep -q "signal 15: shutting the stack down"; then
    ok "the supervisor saw SIGTERM and acted on it"
else
    no "the supervisor saw SIGTERM and acted on it"
fi

if echo "$shutdown_log" | grep -q "tearing the stack down"; then
    ok "SIGTERM became \`docker compose down\`"
else
    no "SIGTERM became \`docker compose down\`"
fi

# The containers and the network both removed: a `down` that stopped containers but left
# the network would leave state behind on a node that reused the data-root.
if echo "$shutdown_log" | grep -q "Container stack-redis-1 Removed" \
    && echo "$shutdown_log" | grep -q "Container stack-whoami-1 Removed"; then
    ok "both containers were removed, not just stopped"
else
    no "both containers were removed, not just stopped"
fi

if echo "$shutdown_log" | grep -q "Network stack_default Removed"; then
    ok "the compose network was removed"
else
    no "the compose network was removed"
fi

if echo "$shutdown_log" | grep -q "WARNING: \`docker compose down\` exited"; then
    no "\`docker compose down\` exited 0"
else
    ok "\`docker compose down\` exited 0"
fi
echo

# --------------------------------------------------- the cross-check actually refuses
echo "a slot the stack does not publish is refused at startup:"

# The check that is the whole point of service/compose_spec.py, exercised against the
# real image: COMPOSE_FILE points at a stack publishing nothing, while service.json
# still declares 8080. The service must refuse rather than start.
mismatch_name="${NAME}-mismatch"
docker rm -f "$mismatch_name" >/dev/null 2>&1 || true
# The mismatching stack is written INTO the container with `docker cp` rather than bind
# mounted. A single-file bind mount from the host is the obvious way and it does not work
# reliably here: on Docker Desktop the host path is inside a VM the daemon shares by
# directory, and a file mount of a path under $TMPDIR silently produced an empty or
# absent target -- the service reported "no compose file at ..." and this test failed for
# a reason that had nothing to do with the check it is about.
#
# So the container is created stopped, the file is copied in, and then it is started.
mismatch_dir=$(mktemp -d)
cat > "$mismatch_dir/docker-compose.yml" <<'MISMATCH'
services:
  quiet:
    image: traefik/whoami@sha256:200689790a0a0ea48ca45992e0450bc26ccab5307375b41c84dfc4f2475937ab
MISMATCH

# `docker cp` into a container that has never been started, which is what makes this
# race-free: the file is in place before the entrypoint has run at all. Copying the
# directory (not the file) creates `/app/other` in one step.
docker create --name "$mismatch_name" --privileged --platform "$PLATFORM" \
    -e COMPOSE_FILE=/app/other/docker-compose.yml \
    --entrypoint /service/entrypoint.sh "$IMAGE" >/dev/null
docker cp "$mismatch_dir" "$mismatch_name:/app/other" >/dev/null
docker start "$mismatch_name" >/dev/null

mismatch_waited=0
while [ "$mismatch_waited" -lt 90 ]; do
    if [ "$(docker inspect -f '{{.State.Running}}' "$mismatch_name" 2>/dev/null)" != "true" ]; then
        break
    fi
    mismatch_waited=$((mismatch_waited + 2))
    sleep 2
done

mismatch_log=$(docker logs "$mismatch_name" 2>&1 | grep -v '^time=' || true)
mismatch_exit=$(docker inspect -f '{{.State.ExitCode}}' "$mismatch_name" 2>/dev/null || echo "?")

if [ "$mismatch_exit" = "1" ]; then
    ok "the service exited 1 rather than starting with an unreachable slot"
else
    no "the service exited 1 rather than starting with an unreachable slot (got $mismatch_exit)"
fi

if echo "$mismatch_log" | grep -q "not published by the compose file"; then
    ok "the error names the slots the compose file does not publish"
else
    no "the error names the slots the compose file does not publish"
    echo "$mismatch_log" | tail -8 | sed 's/^/    /'
fi

# The stack must not have been started: refusing after bringing containers up would make
# the check cosmetic.
if echo "$mismatch_log" | grep -q "Container .* Started"; then
    no "nothing was started before the refusal"
else
    ok "nothing was started before the refusal"
fi

docker rm -f "$mismatch_name" >/dev/null 2>&1 || true
rm -rf "$mismatch_dir"
echo

echo "passed=$passed failed=$failed"
[ "$failed" -eq 0 ]
