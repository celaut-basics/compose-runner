# compose-runner

A [Celaut](https://github.com/celaut-project/nodo) service that runs an existing
`docker-compose.yml` application as a single service instance.

## Why this exists

A Celaut service is one microVM, built from one `.service/Dockerfile`. Most real
applications are a `docker-compose.yml` with two or three services: an app and a
database, or a worker and a queue. Without this service, the only way onto a nodo is
to merge that stack into a single Dockerfile by hand, and to do it again each time
upstream changes the stack.

`compose-runner` does that translation once, for all stacks. It takes the compose file
and the images or build contexts it names, and runs the stack inside the one microVM
of the service. A multi-container application then packs and runs with no merge of
its Dockerfiles.

## How it works

The service runs Docker (Docker-in-Docker) inside its own microVM. At start, it runs
`docker compose up` on a compose file that is packed with the service. The internal
compose network stays internal. The ports that callers must reach are published by the
compose file and declared as API slots in `.service/service.json`.

This costs more than a native single-container service: a container runtime runs
inside the sandbox. That is the price of a compose app with no rewrite. If you run a
stack often, a single-purpose Celaut service (as `bitcoin-node` or `ergo-node`) is
still the better choice.

**There is no proxy.** The compose file's `ports:` bind on the interface of the
microVM, and that interface is the address of the service. So a published port is
the API slot. The service checks that the two lists agree, see
[Ports and slots](#ports-and-slots).

The parts:

- `service/entrypoint.sh` is the `init.entry_path`. It checks the image, refuses to
  run as non-root (with the reason), sets `PATH`, reports the cgroup version, selects
  the `iptables` backend, and starts the supervisor under `docker-init` (tini).
- `service/supervisor.py` starts the health slot, writes a resolver file if the guest
  has none, starts `containerd`, then `dockerd` (`overlay2`, else `vfs`), runs
  `docker load` on the packed image tars, validates, starts the stack with `--wait`,
  and watches dockerd. SIGTERM becomes `docker compose down`.
- `service/compose_spec.py` is the slot/port cross-check. It reads
  `docker compose config --format json`.
- `service/health.py` is the `GET /health` slot. It reports the phase of the launch
  and `docker compose ps`.
- `service/config.py` is the environment contract.
- `.service/preflight.py` does the same cross-check at **build** time, so a mismatch
  fails `nodo pack` and not a node.
- `stack/` is an example stack (an HTTP service and redis). The tests use it. An
  operator replaces it.

**Not implemented:** Podman-in-Podman, `docker compose build` of stacks that need
BuildKit secrets, swarm or overlay networks, and per-container CPU limits (the guest
kernel cannot enforce them, see [finding 1](NODE-REQUIREMENTS.md)).

**The guest kernel has two more gaps** ([finding 8](NODE-REQUIREMENTS.md)), found by
resolving its configuration for both architectures:

- No iptables `raw` table, on arm64 and x86_64. dockerd 28+ needs it for each
  container. The supervisor detects this and starts dockerd with
  `DOCKER_INSECURE_NO_IPTABLES_RAW=1`.
- No `bpf(2)` on **x86_64**. On cgroup v2, runc then refuses each container that is
  not privileged. The service cannot fix this; it logs a warning. Until the nodo guest
  kernel has `CONFIG_BPF_SYSCALL`, use an arm64 node, or a stack of privileged
  containers.

## Use it with your own stack

You need a nodo with a packer backend (see `docs/skill/SKILL.md` and
`docs/PACKING.md` in the nodo repository).

1. Get the template:

   ```sh
   git clone https://github.com/celaut-basics/compose-runner && cd compose-runner
   ```

2. Replace the example stack. Put your compose file, its build contexts, and a
   `stack/.env` (if it needs one) in `stack/`:

   ```sh
   rm stack/docker-compose.yml
   cp /path/to/your/docker-compose.yml stack/
   ```

3. Edit `.service/service.json`:
   - Make `api` list each port that callers must reach. Each one must be published by
     the compose file. Keep the health slot (9000).
   - Set `architecture` to the architecture of the host that packs and runs it
     (`linux/arm64` or `linux/amd64`). The Dockerfile has pinned checksums for both.
     **On an x86_64 node, the current guest kernel cannot start a container that is
     not privileged** ([finding 8](NODE-REQUIREMENTS.md)).
   - Add to `envs` each variable of your stack that you want to set at launch. nodo
     does not enforce this list: `nodo execute -e` gives any variable. The list tells
     the reader of `service.json` which names the service reads.
   - Raise `resources.at_init.disk_space` if your images are large. See
     [Resources](#resources).

4. Pack it. The command prints the service id (a content hash):

   ```sh
   nodo pack .
   ```

5. Check that the node can run it, then start it:

   ```sh
   nodo estimate <service id>
   nodo execute <service id>
   nodo execute -e COMPOSE_UP_TIMEOUT_S 1200 -e POSTGRES_PASSWORD s3cret <service id>
   ```

6. Find the instance and reach it:

   ```sh
   nodo instances                         # id (also the token) and API address
   curl http://<instance address>:9000/health
   nodo tunnel <instance id> 8080         # to reach a slot from another host
   ```

7. Stop it:

   ```sh
   sudo nodo kill <instance id>
   ```

Do not use `nodo ggconf` to test this service. It runs the code on the host, and this
service needs root and its own dockerd. Use `tests/test_image.sh` (below) before you
pack.

The service id identifies **your stack**: the compose file and all files in `stack/`
are hashed into it. Two operators who pack two different compose files get two
different services. That is the purpose of the template shape.

### Run it locally, without a node

```sh
docker buildx build --platform linux/arm64 -f .service/Dockerfile -t compose-runner:test --load .

# --privileged is necessary: dockerd must create bridges, write iptables rules and
# mount overlays. The Dockerfile sets no ENTRYPOINT, because nodo reads it from
# `init.entry_path` in service.json. So give it here.
docker run -d --privileged -p 8080:8080 -p 9000:9000 \
    --entrypoint /service/entrypoint.sh compose-runner:test
```

On an x86_64 host, use `--platform linux/amd64`.

### Ports and slots

| | |
|---|---|
| a port in the compose file's **`ports:`** | binds on the interface of the microVM |
| an entry in **`api`** in `.service/service.json` | what the node advertises and forwards to that interface |

The two must agree, and **only this service checks that**. The node never reads the
compose file, and compose never reads `service.json`. A slot at 8080 for a stack that
publishes 8081 gives an instance that boots, reports healthy, advertises a slot, and
refuses every connection.

So the service checks it twice:

- at **build** time (`.service/preflight.py`, the last `RUN` in the Dockerfile). A
  mismatch fails `nodo pack` before a service id exists.
- at **start** (`service/compose_spec.py`), against `docker compose config --format
  json`. That is compose's own normalisation, so `${VAR}` interpolation and short
  forms are read as compose reads them.

The check has **one direction**: each slot must be published, but a published port
need not be a slot. A metrics port, or a port published for a sibling container, is
normal. A refusal would make you edit the upstream compose file that you came here
not to edit. `expose:` is not read as published, because only the compose network
can reach it.

The health slot (9000) is exempt: this service serves it. A stack that publishes the
health port is refused, because the two would collide. To use another port, set
`HEALTH_PORT` and change the health slot in `service.json` to the same port.

### Health

```sh
curl http://<instance>:9000/health
```

```json
{"status": "ok", "phase": "running", "containers": [
   {"name": "stack-redis-1",  "service": "redis",  "state": "running"},
   {"name": "stack-whoami-1", "service": "whoami", "state": "running"}],
 "running": 2, "total": 2, "expected": 2}
```

The slot answers from the first second of the launch:

| `phase` | status | meaning |
|---|---|---|
| `starting`, `dockerd`, `loading-images`, `validating` | 503 `starting` | no containers yet |
| `compose-up` | 503 | `docker compose up --wait` runs (mostly image pulls) |
| `running` | 200 `ok`, or 503 `degraded` / `starting` | the containers, from `docker compose ps` |
| `failed` | 503 `failed` | the launch failed; the supervisor stops and the guest stops |

`200` means: every container that compose knows is running, and there are as many as
the compose file declares. A container with a `healthcheck:` that reports `unhealthy`
gives 503 even though its state is `running`.

The slot exists because **"the VM is up" and "the stack is up" are different here**.
An instance boots a kernel, starts dockerd, pulls images and starts N containers.
That takes tens of seconds on a cold instance, and each step can fail. During that
time the address of the microVM answers. Without this slot, a caller cannot tell a
slow start from a broken stack.

The slot is on its own port, not a path on a stack port. The stack port belongs to
the stack, and a Postgres slot has no `/health` to add.

One `docker compose ps` answer serves all requests for 2 s, so a burst of requests
does not start a burst of processes.

## The environment it reads

Set these with `nodo execute -e <name> <value>`. nodo puts each variable in
`__config__`. It also gives it to the entrypoint as a real environment variable, if the
name is a C identifier, is not reserved (`PATH`, `LD_PRELOAD` and some others), and the
value is at most 32 KiB (`src/utils/guest_env.py` in nodo).

| variable | default | what it is |
|---|---|---|
| `COMPOSE_FILE` | `/app/stack/docker-compose.yml` | Absolute path in the image. A list of paths is refused: the cross-check must validate the document that runs. |
| `COMPOSE_PROJECT_NAME` | `stack` | Prefix of each container, network and volume. Must match compose's rule `[a-z0-9][a-z0-9_-]*`. |
| `COMPOSE_UP_TIMEOUT_S` | `600` | Time limit for `docker compose up --wait`. On a cold instance this is mostly image pulls. |
| `COMPOSE_DOWN_TIMEOUT_S` | `30` | Container grace time for `docker compose down`. |
| `DOCKERD_TIMEOUT_S` | `60` | Time limit for containerd to start, then the same limit for dockerd. It is separate from the compose limit, so the log tells "the runtime did not start" from "the stack did not start". |
| `DOCKERD_STORAGE_DRIVER` | `auto` | `auto` \| `overlay2` \| `vfs`. `auto` tries overlay2, then vfs, and logs which. |
| `DOCKERD_DATA_ROOT` | `/var/lib/docker` | Where images and layers go. **Must be on the rootfs**, see below. |
| `DNS_SERVERS` | `1.1.1.1 8.8.8.8` | Up to three IP addresses for `/etc/resolv.conf`. See [The network](#the-network-it-asks-for). |
| `HEALTH_PORT` | `9000` | The health slot. Must match the slot in `service.json`. |
| `IPTABLES_BACKEND` | `legacy` | `legacy` \| `nft`. Read by the entrypoint. [Finding 1](NODE-REQUIREMENTS.md) explains the default. |
| `DOCKER_INSECURE_NO_IPTABLES_RAW` | set to `1` only if the kernel has no iptables `raw` table | Read by dockerd. The supervisor sets it when `iptables -t raw -S` fails, and keeps a value that you give. [Finding 8](NODE-REQUIREMENTS.md). |

A value that is not valid stops the service at start, with the reason. For example,
`COMPOSE_UP_TIMEOUT_S=abc` is refused. It does not become 600.

**Your stack's own variables** go in `stack/.env` (compose reads it), or you give them
at launch with `-e` (and list them in `envs`). The children of the supervisor **inherit** its
environment, so `${VAR}` interpolation in the compose file sees those values. The
supervisor sets `DOCKER_HOST` and `PATH` itself, so an inherited value cannot send
the stack to another daemon.

## Where the images live, and why

`/var/lib/docker`, on the **rootfs**. This is the most important default of the
service, and it comes from nodo:

- The rootfs of a writable service is an ext4 image. The node sizes it once, at boot,
  to at least `at_init.disk_space`.
- `/run` is always a tmpfs, and `/tmp` is a tmpfs on a read-only rootfs. A tmpfs is
  RAM. A data-root there would charge each image layer against `mem_limit`. A 1.5 GB
  image on a 1 GB instance would not fill a disk, it would OOM the guest.

For the same reason this service does **not** declare `read_only_filesystem: true`.
With a read-only rootfs, each write outside `/tmp` goes to a tmpfs (RAM), and
`at_init.disk_space` becomes a ceiling, not a floor. Full reasoning, with file
references: [finding 2](NODE-REQUIREMENTS.md).

## The network it asks for

**`["*"]`, open egress**, because this service pulls container images, and the hosts
cannot be listed in advance. A registry answers the manifest and then redirects layer
blobs to a CDN (Docker Hub to `production.cloudflare.docker.com`, ghcr.io to
`pkg-containers.githubusercontent.com`). The compose file decides which registries,
and it does not exist when this template is packed.

A hostname tag would not work. The node resolves the tag itself and opens only those
addresses. It serves no DNS and opens no port 53. dockerd must resolve registry names
itself.

**A nodo guest has no resolver from the node.** nodo writes no `/etc/resolv.conf`.
The pinned debian base layer has `/etc/resolv.conf` as a regular file with
`nameserver 1.1.1.1` and `nameserver 1.0.0.1` (read from `docker save`, not from
`docker run`). The supervisor keeps a file that already names a server. If the packed
filesystem has no nameserver, it writes `DNS_SERVERS`. An explicit `DNS_SERVERS`
always replaces the file. The `*` grant makes those public resolvers reachable.
dockerd also gives these servers to the containers of the stack. Whether `nodo pack`
keeps the debian file is not proven.

**You can use no egress at all.** Run `docker save` on your images into
`stack/images/*.tar` before you pack. The supervisor loads them before compose runs.
The image **bytes** are then part of the content-addressed service. That is stronger
than a digest in a compose file, which only points at a registry. The stack then runs
with no egress. Details: [`stack/images/README.md`](stack/images/README.md).

An operator who wants the node to enforce the boundary uses `service_networks` in
`config.yaml`. A whitelist must list `"*"`, and `blacklist: ["*"]` refuses this
service.

## Resources

**Memory: 1 GB at init, 4 GB at most.** dockerd and containerd use about 120 MB
before your containers start.

**Disk: 4 GB, fixed.** The node sizes the rootfs once, from `at_init.disk_space`.
`at_most.disk_space` does not grow it, so it is set to the same value. The exported
filesystem is about **393 MB** (measured with `docker buildx build -o type=tar` on
arm64). Most of it is the Docker binaries: `dockerd` 95 MB, the CLI 42 MB,
`containerd` 36 MB, `docker-compose` 30 MB, `runc` 14 MB. **The rest is for the images
of your stack**, which are written to this disk. One application image is often
200 MB to 1.5 GB. On `vfs` a stack costs several times more, because vfs copies each
layer. Packed image tars cost about twice their size (the tar and the loaded layers).
For a stack of large images, raise `at_init.disk_space` in `.service/service.json`.

## Everything is pinned

| | pinned by |
|---|---|
| `debian:bookworm-slim` | multi-arch index digest `sha256:88200866…a4171` (the same one `ergo-node` and `yt-transcript` pin) |
| Docker 29.8.1 static bundle | SHA-256 of the published tarball, for `aarch64` and `x86_64` |
| `docker compose` v5.5.1 | the SHA-256 from docker/compose's published `checksums.txt`, for both architectures |
| Debian packages | exact patch versions, read from the mirror (the same strings on arm64 and amd64) |
| the example stack's images | index digests, not tags |

No `latest` anywhere. Two limits:

- **download.docker.com publishes no checksum file next to the static tarballs**
  (`.tgz.sha256` is a 404). The engine pin is an artifact in a reviewed file. It is
  not compared against an upstream document.
- **A Debian package pinned to its patch version** stops the build when the mirror
  drops that version, until this file is edited.

Two package pins were build failures first: `e2fsprogs` is `1.47.0-2+b2` (a binary
rebuild, numbered separately) and `xz-utils` is `5.4.1-1+deb12u2` (the update after
CVE-2024-3094).

## Tests

```sh
sh tests/run.sh          # 250 offline tests, about 6 s, no Docker
sh tests/test_image.sh   # 40 checks on a built image; needs --privileged and the network
```

The offline suite needs **Python 3 and nothing else**: no pytest, no venv, no
network, no Docker. That is the same dependency list as the service.

What they cover:

- **The packer context** (`test_packing.py`). The tests make the build context that
  nodo's packer makes (the `COPY ./x` to `COPY service/x` rewrite, `include`, and
  the recursive `ignore`), and check each COPY source against it. They also check the
  entry path, the `PATH` the entrypoint sets, tini as PID 1, and the checksums for
  both architectures.
- **The slot/port cross-check** (`test_compose_spec.py`): a slot that nothing
  publishes, a protocol mismatch, `expose:` read as published, port ranges,
  `published: ""`, `published: true` (port 1 without a guard, because `bool` is an
  `int` in Python), and a stack that publishes the health port.
- **The environment contract** (`test_config.py`): each default, each refusal, the
  limits, and that `envs` lists each variable the service reads.
- **The health slot** (`test_health.py`), against **three real captures** of
  `docker compose ps --format json`, the HTTP contract on a real socket, and the
  2 s cache.
- **The supervisor** (`test_supervisor.py`), with each subprocess replaced: the
  overlay2 to vfs fallback, argv always a list, `DOCKER_HOST` and `PATH` not
  inherited, the `raw` table switch and the `bpf(2)` warning, the resolver file, the health phases, a packed tar that fails to load is
  fatal, and SIGTERM becomes `compose down`.
- **The build-time reader** (`test_preflight.py`). A construct that it cannot parse
  gives a note, and a note turns a failure into a warning. A partial parser must not
  refuse a correct build.
- **The image** (`test_image.sh`). The service starts with the `PATH` that nodo's
  guest `/init` gives, not the image's `ENV`. The tests check that dockerd starts,
  that the stack starts, that the published port answers **from outside the
  container**, that redis answers *inside* the stack and not outside it, that one
  container finds another by service name, that health goes to 503 when the stack
  stops, that SIGTERM removes both containers and the network, and that a slot that
  is not published is **refused before anything starts**.

### Bugs that the tests or the review found

- **The compose plugin was in a directory that the Docker CLI does not search.** At
  `/opt/docker/cli-plugins`, each `docker compose` at runtime failed with
  `unknown flag: --file`. It is now in `/usr/local/lib/docker/cli-plugins`, and
  `test_image.sh` runs `docker compose version` *through the CLI*.
- **Two Debian version pins did not exist.** See above.
- **`nodo pack` could not build the Dockerfile.** The packer rewrites
  `COPY ./.service/service.json` to `COPY service/.service/service.json`, and
  `service/` holds only what `include` lists. `.service/service.json` and
  `.service/preflight.py` are now in `include`, and `test_packing.py` checks it.
- **dockerd could not find containerd under nodo.** nodo applies no `ENV` from the
  Dockerfile, and the guest `/init` PATH has no `/opt/docker/bin`. The entrypoint and
  the supervisor now set `PATH`.
- **Image pulls could not resolve a registry under nodo.** The guest has no
  resolver. The supervisor now writes one (`DNS_SERVERS`).
- **dockerd never started on a node that emulates arm64.** dockerd waits a fixed 15 s
  for the containerd that it starts itself (moby
  `daemon/internal/containerd/server/supervisor/remote_daemon.go`, `startupTimeout`).
  Under QEMU TCG, containerd needs more time to load its plugins, so dockerd exited
  with `timeout waiting for containerd to start` for overlay2 and for vfs. The
  supervisor now starts containerd, waits for its socket for `DOCKERD_TIMEOUT_S`, and
  gives the socket to dockerd with `--containerd`. Found by the first `nodo execute`.
- **The reap loop took exit statuses from the health thread.** `waitpid(-1)` in the
  supervisor could take the status of a `docker compose ps` child. tini is now PID 1
  and does the reaping.

## What was verified by running it

The first version was run on Apple Silicon (`linux/arm64`, Docker Engine 29.5.2) with
`docker run --privileged`:

- Docker-in-Docker worked end to end. Both containers were healthy in **5.3 s**.
- The overlay2 to vfs fallback occurred on that host (`driver not supported:
  overlay2`), was logged, and vfs worked.
- The published port answered from outside the container. Redis answered inside the
  stack and not outside it. Another container reached it as `redis:6379`.
- Health gave 200, then **503 after the containers stopped**.
- `docker stop` returned in **1 s** with exit code **0**, and removed both containers
  and the network.
- A slot that was not published was refused with exit **1**, before a container was
  created.

The audit against nodo `dev` (`698e6583`) built the image again (`linux/arm64`, Colima,
Docker 29 in the VM) and ran it:

- `tests/test_image.sh`: **40 passed, 0 failed**, with the `PATH` of the nodo `/init`.
  This includes tini as PID 1, the legacy backend, the cross-check, the published port
  from outside, the 503 after stop, and SIGTERM to `compose down`.
- With `/var/lib/docker` on a volume (ext4, as the rootfs of a nodo guest) and an
  empty `/etc/resolv.conf`: the supervisor wrote `1.1.1.1` and `8.8.8.8`, dockerd
  started with **overlay2** (no fallback), the images were pulled through those
  resolvers, and the stack was up in 2.6 s. That empty file is a test mount, not the
  debian layer. Without the volume, overlay2 failed on the overlay of the outer
  container and vfs was used, as before.
- The SHA-256 of the four pinned downloads (Docker 29.8.1 and compose v5.5.1, `aarch64`
  and `x86_64`) were compared with the published artifacts. All four agree.

**Not verified:**

- **This has never run under a real nodo.** It has not been through `nodo pack`, the
  firewall, or a microVM. The guest-kernel findings in
  [`NODE-REQUIREMENTS.md`](NODE-REQUIREMENTS.md) come from Kconfig, not from a booted
  guest. The `raw` table switch and the `bpf(2)` warning of finding 8 could not be
  tested, because the kernel of the test host has both.
- amd64 (no image was built), the `stack/images/*.tar` path with a real tar, stacks
  with `build:` contexts, and long runs.

## What is deliberately not here

- **Privilege drop.** The other services in `celaut-basics` drop to an unprivileged
  uid. This one cannot: dockerd needs root in its namespace. Rootless dockerd needs
  `/dev/fuse` and uid maps that the spec has no field for. The boundary is the
  **microVM**, not a uid inside it. [Finding 4](NODE-REQUIREMENTS.md).
- **`read_only_filesystem: true`.** dockerd needs a writable data-root that is not
  RAM. [Finding 2](NODE-REQUIREMENTS.md).
- **A proxy.** compose binds on the interface of the VM, and that is the address of
  the service. The cross-check replaces the proxy.
- **`ctr`.** It is containerd's debug CLI, and nothing here uses it.
- **A GPU.** `celaut.Sysresources` has no accelerator field.
- **Persistence.** No volume survives the instance. `nodo kill` stops the whole VM
  (SIGKILL to the hypervisor) and its disk goes with it, so the guest gets no signal
  and `docker compose down` does not run. A stack whose database matters must know
  that it is empty at each launch.
