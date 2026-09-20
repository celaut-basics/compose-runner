# compose-runner

A [Celaut](https://github.com/celaut-project/nodo) service that runs an existing
`docker-compose.yml` application as a single service instance.

## Why this exists

A Celaut service is one container, built from one `.service/Dockerfile`. Most
real applications are not that -- they are a `docker-compose.yml` with two or
three services wired together (an app plus a database, a worker plus a queue),
and today the only way onto a nodo is rewriting that stack into a single
Dockerfile by hand, which is real work and has to be redone every time upstream
changes theirs.

`compose-runner` is that translation done once, generically: it takes the
compose file and the images/build contexts it names, and runs the stack inside
the one container a Celaut service is given, so a multi-container application
packs and executes without anyone hand-merging its Dockerfiles first.

## How it is meant to work

The service itself runs Docker (Docker-in-Docker, or an equivalent such as
Podman-in-Podman) inside its own sandbox, and on startup runs `docker compose
up` against a compose file shipped with the packed service. The internal
compose network stays internal; whatever ports the stack needs reached from
outside are declared as this service's own API slots, in the same
`.service/service.json` shape every other repo in `celaut-basics` uses, and
proxied in to the right internal container and port.

That trade is worth being explicit about: nesting a container runtime inside
the sandbox is heavier than a native single-container service, and it is the
price of onboarding a compose app without rewriting it. A stack worth running
often enough is still a better candidate for a proper single-purpose Celaut
service, built the way `bitcoin-node` or `ergo-node` are.

## What is implemented

All of the above, with Docker-in-Docker (not Podman), and one correction to the
design sketch: **there is no proxying.** The compose file's `ports:` bind on the
microVM's own interface, and that interface *is* the service's address, so a published
port already **is** the API slot. Nothing forwards anything a second time. What the
service does instead is **check that the two agree** — see
[Ports and slots](#ports-and-slots).

Concretely:

- `service/entrypoint.sh` — PID 1. Checks the image, refuses to run non-root (with the
  reason), reports the cgroup version, selects the `iptables` backend, hands over.
- `service/supervisor.py` — starts `dockerd`, falls back `overlay2` → `vfs`, `docker
  load`s any packed image tars, validates, brings the stack up with `--wait`, supervises,
  reaps orphans, turns SIGTERM into `docker compose down`.
- `service/compose_spec.py` — the slot/port cross-check, reading `docker compose config
  --format json`.
- `service/health.py` — a `GET /health` slot reporting `docker compose ps`.
- `service/config.py` — the environment contract.
- `.service/preflight.py` — the same cross-check at **build** time, so a mismatch fails
  `nodo pack .` rather than a node.
- `stack/` — an example stack (nginx-style HTTP service + redis) that the tests use and
  that an operator replaces.

**Not implemented:** Podman-in-Podman, `docker compose build` of stacks that need
BuildKit secrets, swarm/overlay networking, and per-container CPU limits (the guest
kernel cannot enforce them — [finding 1](NODE-REQUIREMENTS.md)).

## Using it with your own stack

```sh
git clone https://github.com/celaut-basics/compose-runner && cd compose-runner
rm stack/docker-compose.yml
cp /path/to/your/docker-compose.yml stack/
# plus any build contexts it references, and a stack/.env if it needs one
$EDITOR .service/service.json     # make `api` match your published ports
nodo pack .                       # prints the service id (a content hash)
```

The packed service id identifies **your stack**: the compose file and everything beside
it in `stack/` is hashed into it. Two operators packing two different compose files get
two different services, which is the point of the template shape rather than a
limitation of it.

Locally, without a node:

```sh
docker buildx build --platform linux/arm64 -f .service/Dockerfile -t compose-runner:test --load .

# --privileged is required: dockerd must create bridges, write iptables rules and mount
# overlays. The entrypoint is named explicitly because the Dockerfile deliberately sets
# no ENTRYPOINT -- nodo reads it from `init.entry_path` in service.json instead.
docker run -d --privileged -p 8080:8080 -p 9000:9000 \
    --entrypoint /service/entrypoint.sh compose-runner:test
```

### Ports and slots

| | |
|---|---|
| a port in the compose file's **`ports:`** | binds on the microVM's interface |
| an entry in **`api`** in `.service/service.json` | what the node advertises and forwards to that interface |

They have to match, and **nothing in the system checks that but this service**: the node
never reads the compose file, and compose never reads `service.json`. A slot declared at
8080 against a stack publishing 8081 produces an instance that boots, reports healthy,
advertises a slot and refuses every connection.

So it is checked twice:

- at **build time** (`.service/preflight.py`, the last `RUN` in the Dockerfile) — a
  mismatch fails `nodo pack .` before a service id exists;
- at **startup** (`service/compose_spec.py`) — against `docker compose config --format
  json`, which is compose's own normalisation, so `${VAR}` interpolation and short forms
  are read exactly as compose reads them.

The check is deliberately **one-directional**: every slot must be published, but a
published port need not be a slot. A metrics port, or a port published for a sibling
container's benefit, is ordinary and is left alone — refusing it would force you to edit
the upstream compose file you came here not to edit. `expose:` is not read as published,
because it is reachable only inside the compose network.

The health slot (9000) is exempt: this service serves it, no container publishes it.

### Health

```sh
curl http://<instance>:9000/health
```

```json
{"status": "ok", "containers": [
   {"name": "stack-redis-1",  "service": "redis",  "state": "running"},
   {"name": "stack-whoami-1", "service": "whoami", "state": "running"}],
 "running": 2, "total": 2, "expected": 2}
```

That is a real response, copied from a run. `200` when every container compose knows
about is running and there are as many as the compose file declares; `503` with
`status: "starting"` or `"degraded"` otherwise.

It exists because **"the VM is up" and "the stack is up" are different things here**, and
without this slot a caller cannot tell them apart. An instance boots a kernel, starts
dockerd, pulls images and starts N containers — tens of seconds on a cold instance, and
it can fail at any step. Throughout, the microVM's address answers and the node considers
the instance launched. A caller hitting a stack slot in that window gets a TCP failure
indistinguishable from a broken stack.

Its own slot rather than a path on the stack's port, because the stack's port belongs to
the stack: injecting a `/health` route into someone else's HTTP server would be modifying
the application this service promised not to modify — and a Postgres slot has no
`/health` to add.

A container with a `healthcheck:` in your compose file that reports `unhealthy` makes
this 503 even though its state is `running`, which is the whole point of your having
written the check.

## The environment it reads

| variable | | what it is |
|---|---|---|
| `COMPOSE_FILE` | `/app/stack/docker-compose.yml` | Absolute path, inside the image. A path-separated *list* is refused rather than silently using the first — the cross-check must validate the document that actually runs. |
| `COMPOSE_PROJECT_NAME` | `stack` | Prefix for every container, network and volume. Validated against compose's own `[a-z0-9][a-z0-9_-]*`, which compose refuses rather than normalises. |
| `COMPOSE_UP_TIMEOUT_S` | `600` | Whole budget for `docker compose up --wait`. On a cold instance this is mostly image pulls. |
| `COMPOSE_DOWN_TIMEOUT_S` | `30` | Container grace on shutdown. |
| `DOCKERD_TIMEOUT_S` | `60` | How long dockerd gets to start serving. Separate from the compose budget so "the runtime never came up" and "the stack never came up" are distinguishable in the log. |
| `DOCKERD_STORAGE_DRIVER` | `auto` | `auto` \| `overlay2` \| `vfs`. `auto` tries overlay2 and falls back to vfs, logging which. |
| `DOCKERD_DATA_ROOT` | `/var/lib/docker` | Where images and layers go. **Must not be under `/tmp`** — see below. |
| `HEALTH_PORT` | `9000` | The health slot. Must match the slot in `service.json`. |
| `IPTABLES_BACKEND` | `legacy` | `legacy` \| `nft`. Read by the entrypoint. Why the default is `legacy` is [finding 1](NODE-REQUIREMENTS.md) and it is not cosmetic. |

Every one is refused loudly rather than clamped: `COMPOSE_UP_TIMEOUT_S=abc` stops the
service at start instead of silently becoming 600. A limit that reverts to its default
when mistyped is not the limit the spec declares.

**Your stack's own variables** go in a `stack/.env` file packed with the stack, which
compose reads itself. The supervisor's child environment is **inherited and then
overridden**, not replaced — deliberately, and unlike `yt-transcript`: `${VAR}`
interpolation is a documented compose feature, and a replaced environment would
interpolate your variables to empty strings. What *is* overridden is `DOCKER_HOST`, so an
inherited value cannot redirect your stack onto another daemon.

## Where the images live, and why it matters

`/var/lib/docker`, on the **rootfs** — not `/tmp`. This is the single most consequential
default in the service and it is a fact about nodo, not a preference:
`bash/build_ch_initramfs.sh` mounts `/tmp` as a **tmpfs**, which is RAM. A data-root
there would charge every image layer your stack pulls against the instance's
**`mem_limit`**, so a 1.5 GB image on a 1 GB instance would not fill a disk — it would
OOM the guest, and `disk_space` in the manifest would describe nothing the service uses.

This is also why this service does **not** declare `read_only_filesystem: true`, despite
that being right for almost every other service in `celaut-basics`. A `read_mode=ro`
rootfs is overlaid with a tmpfs upper layer, so *every* write outside `/tmp` is RAM, and
`at_init.disk_space` inverts from a floor into a ceiling. The full reasoning, with file
and line references, is [finding 2](NODE-REQUIREMENTS.md).

## The network it asks for

**`["*"]` — open egress**, because this service pulls container images and the hosts it
pulls them from cannot be enumerated. A registry answers the manifest itself and
redirects layer blobs to a CDN (Docker Hub → `production.cloudflare.docker.com`, ghcr.io
→ `pkg-containers.githubusercontent.com`), chosen per request. And *which* registries are
involved is decided by a compose file that does not exist when this service is packed.

A hostname tag would not work even if the list existed: the node resolves the tag itself
and opens TCP 80/443 to those addresses while opening **nothing on UDP 53**, and dockerd
resolves registry names for itself. It would be granted hosts it can never look up. Only
the first tag of an entry is used, too. Both are in
[`NODE-REQUIREMENTS.md`](NODE-REQUIREMENTS.md) with file:line references, along with what
changed in nodo #391.

**You can need none of it.** `docker save` your stack's images into `stack/images/*.tar`
before packing; `service/supervisor.py` `docker load`s them before compose runs. That
makes the image **bytes** part of the content-addressed service — strictly stronger than
a digest in a compose file, which only points at a registry — and the service then runs
with no egress at all.

## Resources

**Memory: 1 GB at init, 4 GB at most.** dockerd + containerd idle at ~120 MB before your
containers.

**Disk: 4 GB at init, 12 GB at most.** The exported filesystem is **393 MB** (measured,
`docker buildx build -o type=tar`), nearly all of it Docker's own Go binaries — `dockerd`
95 MB, the CLI 42 MB, `containerd` 36 MB, `docker-compose` 30 MB, `runc` 14 MB. **The
rest of that 4 GB is for your stack's images**, which are written onto this instance's
disk. One application image is routinely 200 MB–1.5 GB, and a stack on `vfs` costs
several times more because vfs copies every layer whole instead of sharing it. A stack of
large images needs these figures raised in `.service/service.json`; that is the edit to
make, and it is one line.

DinD is heavy. That is the trade named in *How it is meant to work*, priced.

## Everything is pinned

| | pinned by |
|---|---|
| `debian:bookworm-slim` | index digest `sha256:88200866…a4171` (the same one `ergo-node` and `yt-transcript` pin) |
| Docker 29.8.1 static bundle | SHA-256 computed from the published tarball |
| `docker compose` v5.5.1 | the SHA-256 from docker/compose's published `checksums.txt` — an upstream document |
| Debian packages | exact patch versions, read from the mirror |
| the example stack's images | index digests, not tags |

No `latest` anywhere. Two caveats stated rather than glossed over. **download.docker.com
publishes no checksum file next to the static tarballs** (verified: `.tgz.sha256` is a
404), so what is pinned for the engine is *an* artifact in a reviewed file, not a checksum
compared against an upstream document — the same caveat `ergo-node` records for the Ergo
jar. And **pinning Debian packages to the patch version** means the build stops when one
leaves the mirror, until this file is edited.

Two of those package pins are worth a note, because both were build failures first:
`e2fsprogs` is `1.47.0-2+b2` (a binary rebuild, numbered separately) and `xz-utils` is
`5.4.1-1+deb12u2` (the post-CVE-2024-3094 update). Pinning the upstream-looking
`1.47.0-2` and `5.4.1-1` is not a looser pin, it is a pin to a version that is not there.

## Tests

```sh
sh tests/run.sh          # 202 offline tests, ~6 s, no Docker
sh tests/test_image.sh   # 38 against a built image, needs --privileged and the network
```

The offline suite needs **Python 3 and nothing else** — no pytest, no venv, no network, no
Docker. That is deliberate: its dependency list is the same as the service's own.

What they cover:

**The slot/port cross-check** (`test_compose_spec.py`), weighted towards the ways a
declaration and a compose file disagree: a slot nobody publishes, a protocol mismatch,
`expose:` mistaken for published, port ranges, `published: ""` (compose picking a port,
which no fixed slot can name), and `published: true` — which is port 1 without an explicit
guard, because `bool` is a subclass of `int` in Python. Plus a test that this repo's own
`stack/` and `.service/service.json` agree.

**The environment contract** (`test_config.py`): every default, every refusal, the
boundaries, and that `envs` in `service.json` lists every variable the service actually
reads.

**The health slot** (`test_health.py`) against **three committed captures** of what
`docker compose ps --format json` really wrote — including the `running` + `unhealthy`
case, which is what proves the summariser does not read `State` alone. Plus the HTTP
contract against a real socket.

**The supervisor's decisions** (`test_supervisor.py`) with every subprocess replaced:
that overlay2 is tried before vfs and an explicitly-chosen driver is not fallen back
from, that argv is always a list and never a shell string, that `DOCKER_HOST` cannot be
inherited, that a packed image tar failing to load is fatal, and that SIGTERM becomes
`compose down`.

**The build-time reader** (`test_preflight.py`), whose contract is "be right or admit you
skipped it": a construct it cannot parse produces a note, and a note downgrades a failure
to a warning — a partial parser must not refuse a correct build.

**The image** (`test_image.sh`): that dockerd starts, that the stack comes up, that its
published port answers **from outside the container**, that the unpublished redis answers
*inside* the stack and not outside it, that one container resolves another by service
name, that health goes 503 when the stack stops, that SIGTERM removes both containers and
the network, and that a deliberately mismatched slot list is **refused before anything
starts**.

### Two bugs the tests found

- **The compose plugin was in a directory the Docker CLI does not search.** At
  `/opt/docker/cli-plugins`, the image built, the build-time smoke test passed (it ran
  the binary directly, which works), and every `docker compose` at runtime failed with
  `unknown flag: --file` — because with no plugin found, `compose` is not a subcommand,
  so the CLI parsed `--file` as an argument to `docker` itself. Nothing in that message
  suggests a misplaced plugin. Found by running the container. It now lives in
  `/usr/local/lib/docker/cli-plugins`, and `test_image.sh` asserts `docker compose
  version` works *through the CLI*, not just that the binary runs.
- **Two Debian version pins did not exist.** See above.

## What was verified by running it

On this machine (Apple Silicon, `linux/arm64`, Docker Engine 29.5.2):

- **Docker-in-Docker works end to end.** dockerd started, pulled both images, and brought
  both containers up **healthy in 5.3 s**.
- **The overlay2 → vfs fallback fired for real**, and is not theoretical: on this host
  dockerd reported `driver not supported: overlay2` (the data-root sits on Docker
  Desktop's own overlay2, and an overlay upper dir cannot be stacked on one), the
  supervisor logged it, fell back, and came up on vfs.
- **The stack's published port answers from outside the container**, through two layers of
  port publishing — compose's inside, Docker's outside. That is the path an API slot takes.
- **The internal compose network works**: `redis` publishes no port, answers `PONG` inside
  the stack, is **not** reachable from outside, and another container reaches it as
  `redis:6379` by name.
- **The health slot reports honestly**: 200 with both containers listed, and **503 once
  the stack's containers were stopped** while the supervisor kept running.
- **SIGTERM is clean.** `docker stop` returned in **1 s**, exit code **0**, with both
  containers *removed* and the compose network removed.
- **A mismatched slot list is refused before anything starts**: exit **1**, an error
  naming the unpublished slot, and no container ever created.
- **The legacy iptables backend is selected and logged.**
- **202 offline tests and 38 image tests pass.**

What is **not** verified: **this has never been launched under a real nodo.** `nodo pack
.` has not been run against it, no service id has been produced, and nothing has been
through `resolve_network`, the firewall, or a real microVM — so the guest-kernel findings
in [`NODE-REQUIREMENTS.md`](NODE-REQUIREMENTS.md) are resolved from Kconfig rather than
observed in a booted guest. Also unverified: any architecture other than arm64, the
offline `stack/images/*.tar` path against a real tar (its logic is unit-tested, not run),
stacks using `build:` contexts, and anything about long-running stability — the longest
this has run is minutes.

Verification used `docker run --privileged`, which exercises dockerd, compose, the
bridge, the published ports and the teardown, but **not** nodo's initramfs, its firewall,
or KVM.

## What is deliberately not here

- **Privilege dropping.** Every other service in `celaut-basics` drops to an
  unprivileged uid; this one cannot — dockerd needs root in its namespace. Rootless
  dockerd needs `/dev/fuse` and uid maps the spec has no field for. The confinement here
  is the **microVM boundary**, not a uid inside it, which is a weaker claim than
  `yt-transcript`'s and is stated as one. [Finding 4](NODE-REQUIREMENTS.md).
- **`read_only_filesystem: true`.** dockerd needs a writable data-root that is not RAM.
  [Finding 2](NODE-REQUIREMENTS.md).
- **A proxy.** The design sketch above says ports are "proxied in to the right internal
  container and port". They are not, because they do not need to be: compose binds on the
  VM's interface and that is the service's address. What replaced the proxy is the
  cross-check.
- **`ctr`.** containerd's debug CLI talks to the same socket with the same authority and
  nothing here invokes it, so it is not copied out of the tarball.
- **A GPU.** `celaut.Sysresources` has no accelerator field, so a stack needing one could
  not be scheduled. Same conclusion `remote-browser` and `yt-transcript` reached.
- **Persistence.** No named volume survives the instance. A Celaut instance has no
  persistent volume for its own data, so a stack whose database matters needs that
  understood: it is empty on every launch.
