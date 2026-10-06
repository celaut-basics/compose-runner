# linux/arm64 variant. nodo builds it with --opt platform=linux/arm64 (from
# service.json), and BuildKit sets TARGETARCH from it. The amd64/ and arm64/
# Dockerfiles differ only in this comment: the base pin is a multi-arch index,
# and the fetch stage picks the Docker and compose tarball for TARGETARCH.

# Every dependency is pinned by digest or by SHA-256, with no `latest` anywhere and no
# package pinned only to its suite. This service is content-addressed -- two packs of
# the same source tree have to produce the same id -- and what this one packs is a
# *container runtime*: if "whatever download.docker.com served today" were acceptable
# here, the service id would identify the compose file and not the thing that runs it.
#
# Two stages, one runtime. Only the final stage's filesystem is exported by nodo's
# packer, so the stage that unpacks and verifies the tarballs is not in the service:
# the runtime holds eight Go binaries, one compose plugin, Python's standard library,
# and the operator's stack.

# ---------------------------------------------------------------------------- base
# Pinned by the index digest, which is the multi-arch one `debian:bookworm-slim`
# resolved to -- the same digest `celaut-basics/ergo-node` and
# `celaut-basics/yt-transcript` pin. Its linux/arm64 member is
# sha256:6bd27d44e6c32a66bbd72d7cb2b76a8ae3497ec2e5274a81abd1b37f6013fa1f; pinning the
# index rather than that member is what lets the amd64/ and arm64/ trees share one
# pin instead of a second digest to look up.
ARG BASE_DIGEST=sha256:88200866dfff7ea7f5cbcb6ec7c8a701889efe6fe859fe64d6990e4b07ea4171

# ------------------------------------------------------------------------ fetch stage
# Where the two archives are verified and unpacked. Separate from the runtime for the
# ordinary reason -- `tar`, `curl` and the tarballs themselves do not need to reach the
# exported filesystem -- and for one specific to this service: the Docker tarball
# contains `ctr` and `containerd-stress`-adjacent tooling this service never runs, and
# unpacking here means choosing what to copy out rather than deleting what not to keep.
FROM debian:bookworm-slim@${BASE_DIGEST} AS fetch

# Not pinned to exact versions, unlike the runtime's packages below, and the asymmetry
# is the reason: nothing installed here reaches the exported filesystem. A `curl` that
# changes cannot change what this service *is* unless it changes what it downloads, and
# what it downloads is verified by SHA-256 before extraction and smoke-tested after.
RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# One Dockerfile for both architectures. BuildKit sets TARGETARCH from the platform
# that nodo's packer passes (`--opt platform=<architecture in service.json>`), so a
# change of `architecture` in .service/service.json is the only edit needed. The
# packer builds only for the architecture of its own host (docs/PACKING.md).
ARG TARGETARCH

# --------------------------------------------------------------------- docker engine
# The **static** binary bundle from download.docker.com, not the `docker-ce` apt
# packages.
#
# The apt route would have been fewer lines and is the wrong shape twice over. It pulls
# a systemd unit, a `containerd.io` package and their dependency trees into an image
# with no init system to run any of it -- nodo execs `init.entry_path` as PID 1 and
# there is no systemd to start `docker.service`. And it installs from a repository whose
# package versions move, so pinning the engine would mean pinning eight packages and
# their transitive dependencies to versions that leave the mirror on the next security
# update. The static bundle is one artifact, one checksum, one line of diff when it
# moves -- the same trade `ergo-node` makes for the Ergo jar and `bitcoin-node` for the
# Core tarball.
#
# The checksums were computed from the published artifacts:
#
#   curl -fsSL https://download.docker.com/linux/static/stable/aarch64/docker-29.8.1.tgz | sha256sum
#   curl -fsSL https://download.docker.com/linux/static/stable/x86_64/docker-29.8.1.tgz | sha256sum
#
# **download.docker.com publishes no checksum file next to the static tarballs** --
# verified, `docker-29.8.1.tgz.sha256` is a 404 -- so unlike the compose plugin below
# there is no upstream document to compare this against. What is pinned is *an*
# artifact, in a reviewed file, which cannot change without a diff. That is weaker than
# a signed sums file and is stated rather than glossed over; the same caveat `ergo-node`
# records for the Ergo jar.
ARG DOCKER_VERSION=29.8.1
ARG DOCKER_SHA256_ARM64=667395fbffab52901b80181dfbb39ea76da2fbd7642c4fbddd24e42146b07b48
ARG DOCKER_SHA256_AMD64=d8db66739d2e28d4933786d73e918d9be643a67fbd835db1bf740d650a259e70

# -------------------------------------------------------------------- compose plugin
# The compose v2 plugin binary, pinned by the SHA-256 from the `checksums.txt` that the
# docker/compose release publishes -- an **upstream document**, which is what the engine
# tarball above does not have:
#
#   curl -fsSL https://github.com/docker/compose/releases/download/v5.5.1/checksums.txt \
#     | grep 'linux-\(aarch64\|x86_64\)$'
#
# Installed as a CLI plugin at `cli-plugins/docker-compose` rather than as a standalone
# `docker-compose`, because `docker compose` (the subcommand) is what this service
# invokes and what current compose documentation describes. The standalone spelling is
# the v1 shape and is not what the plugin protocol expects.
ARG COMPOSE_VERSION=5.5.1
ARG COMPOSE_SHA256_ARM64=732e3a84c1a0f67256ce80bc2598a24546b10ca05f9faa97efceb1171ece2ef7
ARG COMPOSE_SHA256_AMD64=db1889184726840f75c4f9c001048430d4f25b3be3cb084d3ddd762bc0aed576

# Docker and compose spell the architectures `aarch64` and `x86_64`; BuildKit spells
# them `arm64` and `amd64`. An architecture with no pinned checksum stops the build.
# Each download is verified before anything reads it.
RUN set -eu; \
    case "${TARGETARCH}" in \
        arm64) arch=aarch64; docker_sha="${DOCKER_SHA256_ARM64}"; compose_sha="${COMPOSE_SHA256_ARM64}" ;; \
        amd64) arch=x86_64;  docker_sha="${DOCKER_SHA256_AMD64}"; compose_sha="${COMPOSE_SHA256_AMD64}" ;; \
        *) echo "no pinned Docker build for TARGETARCH='${TARGETARCH}'" >&2; exit 1 ;; \
    esac; \
    curl -fsSL --proto '=https' --tlsv1.2 -o /tmp/docker.tgz \
        "https://download.docker.com/linux/static/stable/${arch}/docker-${DOCKER_VERSION}.tgz"; \
    echo "${docker_sha}  /tmp/docker.tgz" | sha256sum -c -; \
    mkdir -p /tmp/docker-unpacked; \
    tar -xzf /tmp/docker.tgz -C /tmp/docker-unpacked --strip-components=1; \
    ls -1 /tmp/docker-unpacked; \
    curl -fsSL --proto '=https' --tlsv1.2 -o /tmp/docker-compose \
        "https://github.com/docker/compose/releases/download/v${COMPOSE_VERSION}/docker-compose-linux-${arch}"; \
    echo "${compose_sha}  /tmp/docker-compose" | sha256sum -c -; \
    chmod 0755 /tmp/docker-compose

# -------------------------------------------------------------------------- runtime
FROM debian:bookworm-slim@${BASE_DIGEST} AS runtime

# What dockerd needs from the distribution at runtime, pinned to the exact versions in
# bookworm at the time this was written -- the same discipline `ergo-node` applies to
# its eight packages. The trade is stated there and holds here: a Debian security update
# cannot silently change this image, and equally the build stops until this file is
# edited when one of these leaves the mirror.
#
# Each one, and why it is not optional:
#
#   iptables        dockerd manages the NAT and filter rules for its bridge network by
#                   shelling out to this. Without it, containers in the stack get an
#                   address on `docker0` and cannot reach anything off it -- so no
#                   image pull from inside a container, and no egress for the stack.
#                   Debian's package installs BOTH the nft and legacy backends and
#                   points the `iptables` alternative at **nft**; `service/entrypoint.sh`
#                   repoints it at legacy, because nodo's guest kernel has
#                   CONFIG_NF_TABLES=y with no address family (see NODE-REQUIREMENTS.md
#                   finding 1). Both are present so either can be selected at runtime.
#   iproute2        `ip link`, `ip addr`. libnetwork uses it to create the bridge and
#                   the veth pairs that attach containers to it.
#   e2fsprogs       dockerd probes its data-root's filesystem before choosing a storage
#                   driver; overlay2's checks want the ext4 tooling present.
#   xz-utils        layer decompression. A registry serving an xz-compressed layer
#                   fails the pull without it, and which compression a layer uses is
#                   the registry's choice, not this image's.
#   procps          `ps`, which is what the supervisor's diagnostics and anyone reading
#                   a wedged instance's console will reach for.
#   ca-certificates dockerd speaks TLS to every registry, and without a trust store
#                   every pull fails certificate verification. NOT pinned to a patch
#                   version, unlike the rest, because the *contents* of a CA bundle are
#                   a moving target by design and pinning one would mean shipping a
#                   trust store that ages out.
#   python3.11      the supervisor and the health slot. `python3.11` and not `python3`:
#                   the `python3` metapackage is a moving pointer, which is the one
#                   thing this file is built to avoid. Stdlib only -- no pip, no
#                   wheels, no requirements.txt.
#
# Deliberately absent: `git` (dockerd only needs it for `docker build` from a git URL,
# which a packed stack cannot use), `pigz` (parallel gzip, a speedup dockerd falls back
# from cleanly), and `fuse-overlayfs`/`uidmap` (the rootless path this service does not
# take -- see NODE-REQUIREMENTS.md finding 4).
#
# The exact strings are what the mirror actually has, read from it rather than guessed:
#
#   apt-cache policy <pkg> | awk '/Candidate:/ {print $2}'
#
# Two carry a suffix that a version guessed from the upstream release would not have,
# and both were build failures before they were comments. `e2fsprogs` is `1.47.0-2+b2`
# -- a binary rebuild of the same source, which Debian numbers separately -- and
# `xz-utils` is `5.4.1-1+deb12u2`, the security update after CVE-2024-3094. Pinning
# "1.47.0-2" and "5.4.1-1" is not a looser pin, it is a pin to a version that is not
# there, and apt fails the build rather than resolving to the nearest one.
RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        iptables=1.8.9-2 \
        iproute2=6.1.0-3 \
        e2fsprogs=1.47.0-2+b2 \
        xz-utils=5.4.1-1+deb12u2 \
        procps=2:4.0.2-3 \
        python3.11=3.11.2-6+deb12u8 \
        python3.11-minimal=3.11.2-6+deb12u8 \
        libpython3.11-stdlib=3.11.2-6+deb12u8 \
        libpython3.11-minimal=3.11.2-6+deb12u8 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3.11 /usr/bin/python3

# The engine, the CLI, and the four helpers dockerd execs.
#
# Copied one by one rather than as a directory, because the tarball carries binaries
# this service never runs and an image should hold what it uses:
#
#   dockerd                    the daemon
#   docker                     the CLI the supervisor invokes
#   containerd                 the runtime dockerd delegates to, exec'd by it
#   containerd-shim-runc-v2    one per container, holds its stdio and exit status
#   runc                       what actually clones the namespaces
#   docker-proxy               userland forwarder for a published port. This is the
#                              binary that makes `ports:` in the compose file work, so
#                              it is the one whose absence would break the published
#                              slots specifically, silently, and only for connections
#                              from outside the VM.
#   docker-init                tini. service/entrypoint.sh runs it as PID 1, to reap
#                              orphans and forward signals to the supervisor. A
#                              packed stack can also set `init: true` on a service.
#
# `ctr` is deliberately not copied: it is containerd's own debug CLI, it talks to the
# same socket with the same authority, and nothing here invokes it.
COPY --from=fetch /tmp/docker-unpacked/dockerd                 /opt/docker/bin/dockerd
COPY --from=fetch /tmp/docker-unpacked/docker                  /opt/docker/bin/docker
COPY --from=fetch /tmp/docker-unpacked/containerd              /opt/docker/bin/containerd
COPY --from=fetch /tmp/docker-unpacked/containerd-shim-runc-v2 /opt/docker/bin/containerd-shim-runc-v2
COPY --from=fetch /tmp/docker-unpacked/runc                    /opt/docker/bin/runc
COPY --from=fetch /tmp/docker-unpacked/docker-proxy            /opt/docker/bin/docker-proxy
COPY --from=fetch /tmp/docker-unpacked/docker-init             /opt/docker/bin/docker-init

# The compose plugin goes in `/usr/local/lib/docker/cli-plugins`, which is one of the
# directories the Docker CLI actually searches -- **not** next to the binaries in
# /opt/docker, which it does not.
#
# This was a real failure, found by running the container and not by reading anything.
# With the plugin at `/opt/docker/cli-plugins/docker-compose`, the image builds, the
# smoke test's `/opt/docker/cli-plugins/docker-compose version` passes (it invokes the
# binary directly, which works fine), and then every `docker compose ...` the supervisor
# runs fails with:
#
#   unknown flag: --file
#   Usage:  docker [OPTIONS] COMMAND [ARG...]
#
# -- because with no plugin found, `compose` is not a subcommand, so the CLI parses
# `--file` as an argument to `docker` itself. The error names the flag rather than the
# plugin, which is what makes it worth a comment: nothing in that message suggests the
# plugin is in the wrong directory.
#
# The CLI searches `$DOCKER_CONFIG/cli-plugins` (per-user) and a small set of system
# directories, of which `/usr/local/lib/docker/cli-plugins` is the one intended for
# locally-installed plugins. `tests/test_image.sh` asserts `docker compose version`
# works through the CLI, not just that the binary runs, so this cannot regress silently.
COPY --from=fetch /tmp/docker-compose                          /usr/local/lib/docker/cli-plugins/docker-compose

# dockerd finds `containerd`, `runc`, `docker-proxy` and the shim by searching PATH, not
# by an absolute path it was compiled with.
#
# **nodo ignores this line.** The packer exports the image as a filesystem, and every
# `ENV` is lost (docs/PACKING.md). The guest /init starts the entrypoint with its own
# PATH, which does not contain /opt/docker/bin. So service/entrypoint.sh sets PATH, and
# the supervisor sets it again for each child. This line only serves `docker run` and
# `docker exec` in local tests.
ENV PATH="/usr/local/sbin:/opt/docker/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# The operator's stack. Copied as a directory so `stack/.env`, `stack/images/*.tar` and
# any build contexts come with it -- the whole point of the template shape is that what
# lands here is the operator's, not this repo's.
COPY ./stack /app/stack

COPY ./service /service

# `.service/service.json` is copied INTO the image, which is unusual and is load-bearing:
# `service/supervisor.py` reads the `api` slot list out of it at startup and refuses to
# start if the compose file does not publish those ports. Reading the real file means
# the declaration the node packed and the declaration this check uses are the same
# bytes; a copy of the port list in an env var would be a second declaration to keep in
# sync, and the failure of that sync is exactly what the check exists to catch.
#
# The packer rewrites every `COPY ./x` to `COPY service/x` (docs/PACKING.md, "COPY Path
# Adjustment"), and `service/` holds only what `include` in pack_config.json lists. So
# `.service/service.json` and `.service/preflight.py` are in `include`. Without them
# this COPY has no source and `nodo pack` fails. tests/test_packing.py checks every
# COPY source against that rule.
COPY ./.service/service.json /.service/service.json

RUN chmod 0755 /service/entrypoint.sh \
    && chmod 0644 /service/*.py \
    && chmod 0644 /.service/service.json \
    && mkdir -p /var/lib/docker /var/lib/docker-cli /usr/local/sbin

# The build-time smoke test: every binary this service will exec, actually exec'd, plus
# the Python modules importing cleanly and the example stack parsing. It runs during the
# build, so an image that cannot possibly work never becomes a service id.
#
# Note the absence of `| head -1` on these. Piping a command into `head` moves the exit
# status to `head`'s, which is always 0, so under `set -e` a binary that fails to start
# is a build that succeeds.
#
# `dockerd --version` rather than starting it: dockerd needs privileges and a cgroup
# tree that a BuildKit step does not have, so what is checkable here is that the binary
# runs and reports itself. That the daemon actually starts is what `tests/test_image.sh`
# checks, under `--privileged`, which is the earliest place it can be checked honestly.
RUN set -eu; \
    /opt/docker/bin/dockerd --version; \
    /opt/docker/bin/docker --version; \
    /opt/docker/bin/containerd --version; \
    /opt/docker/bin/runc --version; \
    /opt/docker/bin/docker-proxy --help 2>/dev/null || true; \
    test -x /opt/docker/bin/containerd-shim-runc-v2; \
    /opt/docker/bin/docker-init --version; \
    /usr/local/lib/docker/cli-plugins/docker-compose version; \
    /opt/docker/bin/docker compose version; \
    /usr/bin/python3 --version; \
    test -x /usr/sbin/iptables-legacy; \
    test -x /usr/sbin/iptables-nft; \
    /usr/sbin/iptables-legacy --version; \
    test -f /app/stack/docker-compose.yml; \
    test -f /.service/service.json; \
    cd /service && /usr/bin/python3 -c "import compose_spec, config, health, supervisor"; \
    /usr/bin/python3 -c "import json; json.load(open('/.service/service.json'))"; \
    echo 'smoke test: every binary runs, the modules import, the declaration parses'

# The slot cross-check, run at build time against the stack that is packed with it.
#
# This is the one check that is worth doing twice -- here and at startup -- because the
# two catch different things. At startup it catches a compose file whose `${VAR}`
# interpolation changed what is published. Here it catches the ordinary case: an
# operator who edited `stack/docker-compose.yml` and forgot `.service/service.json`, or
# the reverse. Failing at `nodo pack .` is failing before a service id exists, which is
# much cheaper than failing on a node after one has been distributed.
#
# It cannot use `docker compose config` -- there is no daemon in a build step and the
# plugin needs none for `config`, but the compose file here may interpolate variables
# only present at runtime. So this parses the compose file with a deliberately tiny
# YAML-subset reader (`.service/preflight.py`) whose *only* job is finding `published:`
# ports, and which says so rather than pretending to be a compose implementation.
COPY ./.service/preflight.py /tmp/preflight.py
RUN /usr/bin/python3 /tmp/preflight.py /app/stack/docker-compose.yml /.service/service.json \
    && rm -f /tmp/preflight.py

# No ENTRYPOINT and no CMD. nodo's packer exports the built image as a *filesystem*
# and ignores both (docs/PACKING.md); what is exec'd comes from `init.entry_path` in
# service.json. A harness that runs `docker run <image>` therefore passes
# `--entrypoint /service/entrypoint.sh`, as tests/test_image.sh and the README do.
