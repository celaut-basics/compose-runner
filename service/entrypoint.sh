#!/bin/sh
# What runs as PID 1, and the three things it does before handing over to Python.
#
# Under nodo's microVM virtualizer this process is execed by the initramfs `/init`
# straight out of `switch_root` (`bash/build_ch_initramfs.sh`), as root, with no init
# system underneath it. There is no runtime that will honour an `ENTRYPOINT` or a
# `USER` line, because the Dockerfile's metadata is not what starts this:
# `init.entry_path` in `.service/service.json` is, and nodo's packer exports the built
# image as a *filesystem* and drops everything else (`docs/PACKING.md`: "the Dockerfile
# is not used to define a running container").
#
# Unlike most services in celaut-basics, this one does **not** drop privileges. That is
# not an oversight and it is the single biggest thing to know about this service:
# dockerd needs root in its namespace to create bridges, write iptables rules, mount
# overlays and clone namespaces. Rootless dockerd exists and needs `/dev/fuse` plus
# uid/gid maps that the spec has no way to declare (NODE-REQUIREMENTS.md, finding 4).
# The confinement this service relies on is therefore the **microVM boundary**, not a
# uid inside it -- which is a weaker claim than yt-transcript's and is stated rather
# than glossed over.
#
# POSIX sh, not bash: nothing here needs bash, and the image does not ship one.

set -eu

log() {
    printf '[compose-runner] %s\n' "$1" >&2
}

fatal() {
    log "FATAL: $1"
    exit 2
}

# ---------------------------------------------------------------- 1. the image is sane
# Checked here rather than assumed, because every one of these failures is much clearer
# as a line on the console than as whatever Python or dockerd says when it is missing.
# The build-time smoke test in .service/Dockerfile checks the same things, so reaching
# any of these at runtime means the filesystem that was packed is not the one that was
# built.
[ -x /opt/docker/bin/dockerd ] || fatal "/opt/docker/bin/dockerd is missing or not executable"
[ -x /opt/docker/bin/docker ] || fatal "/opt/docker/bin/docker is missing or not executable"
[ -x /usr/local/lib/docker/cli-plugins/docker-compose ] \
    || fatal "the docker compose plugin is missing from /usr/local/lib/docker/cli-plugins, which is where the Docker CLI looks for it"
[ -x /usr/bin/python3 ] || fatal "/usr/bin/python3 is missing"

COMPOSE_FILE_PATH=${COMPOSE_FILE:-/app/stack/docker-compose.yml}
[ -f "$COMPOSE_FILE_PATH" ] || fatal "no compose file at $COMPOSE_FILE_PATH. Pack one into stack/."

# ------------------------------------------------------------------ 2. root, and why
# dockerd cannot run as a normal user without the rootless setup this image does not
# ship. Refused explicitly instead of letting dockerd fail thirty lines later with a
# permissions error that reads like a kernel problem.
if [ "$(id -u)" -ne 0 ]; then
    fatal "this service must run as root: dockerd needs to create bridges, write iptables rules and mount overlays. Rootless dockerd needs /dev/fuse and uid maps the Celaut spec cannot declare -- see NODE-REQUIREMENTS.md."
fi

# --------------------------------------------------------- 3. cgroups, checked not fixed
# nodo's initramfs mounts the unified hierarchy for us
# (`mount -t cgroup2 none /newroot/sys/fs/cgroup` in bash/build_ch_initramfs.sh) and it
# does so *non-fatally* -- on failure it logs a warning and carries on, with a comment
# naming this exact class of service as what breaks.
#
# So this checks, and mounts only if nothing is there. Mounting is attempted rather than
# demanded because this same image runs under plain `docker run --privileged` in the
# tests, where the host's cgroup tree is already mounted and may be v1.
if [ ! -d /sys/fs/cgroup ]; then
    mkdir -p /sys/fs/cgroup || fatal "cannot create /sys/fs/cgroup"
fi
if [ -f /sys/fs/cgroup/cgroup.controllers ]; then
    log "cgroup v2 is mounted at /sys/fs/cgroup"
elif [ -d /sys/fs/cgroup/memory ] || [ -d /sys/fs/cgroup/systemd ]; then
    # cgroup v1, which is what a Docker-for-Mac style host often still presents.
    # dockerd supports it; it is logged because which one is in use changes what
    # "Devices cgroup isn't mounted" would mean if it appeared.
    log "cgroup v1 is mounted at /sys/fs/cgroup"
else
    if mount -t cgroup2 none /sys/fs/cgroup 2>/dev/null; then
        log "mounted cgroup2 at /sys/fs/cgroup (nothing was mounted there)"
    else
        # Not fatal here, for the same reason it is not fatal in nodo's /init: the
        # authoritative complaint comes from dockerd, which names the controller it
        # could not find. A guess here would only obscure it.
        log "WARNING: no cgroup filesystem at /sys/fs/cgroup and cgroup2 could not be mounted. dockerd will likely refuse to start with 'Devices cgroup isn't mounted'; see NODE-REQUIREMENTS.md."
    fi
fi

# ------------------------------------------------------------- 4. iptables, explicitly
# The one runtime decision this shell makes rather than checks.
#
# Debian's `iptables` package installs BOTH backends and points the `iptables`
# alternative at `iptables-nft` by default (verified: `update-alternatives --display
# iptables` in bookworm-slim reports "link currently points to /usr/sbin/iptables-nft").
# dockerd shells out to whichever `iptables` is on PATH.
#
# nodo's guest kernel has `CONFIG_NF_TABLES=y` but **no nf_tables address family** --
# `CONFIG_NF_TABLES_IPV4`, `_IPV6` and `_INET` are all absent from the resolved config,
# and `CONFIG_NFT_NAT` is dropped by Kconfig because it depends on them. So
# `iptables-nft` in a nodo guest can create no ipv4 table at all, while
# `CONFIG_IP_NF_IPTABLES=y` + `CONFIG_IP_NF_IPTABLES_LEGACY=y` means the legacy path
# works completely. This is finding 1 in NODE-REQUIREMENTS.md and it is the difference
# between a stack with working egress and one without.
#
# Set here rather than in the Dockerfile with `update-alternatives` so the log says
# which backend this launch used -- and so an operator can override it for a host whose
# kernel is the other way round.
IPTABLES_BACKEND=${IPTABLES_BACKEND:-legacy}
case "$IPTABLES_BACKEND" in
    legacy|nft) ;;
    *) fatal "IPTABLES_BACKEND=$IPTABLES_BACKEND is neither 'legacy' nor 'nft'" ;;
esac
# The six names dockerd and its libnetwork actually invoke, each mapped to the chosen
# backend's real binary. Debian names them `iptables-legacy`, `iptables-legacy-save`,
# `iptables-legacy-restore` and the ip6 equivalents, so the backend infix goes after the
# family and before the verb -- which is why this is a table rather than a string
# substitution that has to know that rule.
mkdir -p /usr/local/sbin
for family in iptables ip6tables; do
    for verb in '' '-save' '-restore'; do
        target="/usr/sbin/${family}-${IPTABLES_BACKEND}${verb}"
        if [ -x "$target" ]; then
            ln -sf "$target" "/usr/local/sbin/${family}${verb}"
        else
            fatal "$target is not in this image, so the $IPTABLES_BACKEND iptables backend cannot be selected"
        fi
    done
done
log "iptables backend: $IPTABLES_BACKEND ($(/usr/local/sbin/iptables --version 2>/dev/null || echo 'not usable'))"

# ------------------------------------------------------------------------- 5. hand over
# `exec`, so the Python supervisor *becomes* PID 1 rather than being a child of this
# shell. That matters twice: a signal the node sends reaches the thing that knows how to
# run `docker compose down`, and the reaping that PID 1 owes its orphans happens in the
# process that has a loop to do it in (see service/supervisor.py, `reap`).
log "handing over to the supervisor"
exec /usr/bin/python3 /service/supervisor.py
