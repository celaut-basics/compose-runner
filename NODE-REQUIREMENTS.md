# What this service needs from the node, and eight things the spec cannot express

`compose-runner` asks for more than any other service in `celaut-basics`, because it
runs a container runtime inside the sandbox rather than a program. This is the list of
what it needs, and then the findings that are really about nodo rather than about this
service — written down because they were found by building against
[`celaut-project/nodo`](https://github.com/celaut-project/nodo) at `upstream/dev`
(`306e3531`), and because the first one would silently produce a stack with no egress at
all. Findings 6 and 7, and the corrections marked "Re-checked", come from a review
against nodo `dev` at `b2f79449`. Finding 8 and the per-architecture corrections come
from an audit against nodo `dev` at `698e6583`.

Everything below about the guest kernel was resolved by running Kconfig itself, not by
reading the fragment: `make ARCH=arm64 defconfig && make ARCH=arm64 kvm_guest.config`
then `merge_config.sh` with `bash/guest-kernel/nodo-guest.config` and
`nodo-guest-arm64.config`, then `olddefconfig`, on linux 6.12.103 — the version
`bash/guest-kernel/build.sh:18` pins. A fragment entry is a *request*: Kconfig silently
drops any symbol whose dependencies are unmet, which is exactly what finding 1 is.

The first resolution was for **arm64 only**. The audit at `698e6583` resolved the same
steps for **x86_64** too (`ARCH=x86_64`, `nodo-guest-x86_64.config`, the sequence of
`bash/guest-kernel/build.sh:82-87`). The two kernels are not the same. The x86_64
`defconfig` has no BPF, so the x86_64 guest has no `CONFIG_BPF_SYSCALL` and no
`CONFIG_CGROUP_BPF`. Finding 8 explains what that stops. The x86_64 configuration was
resolved on an arm64 host. That changes no symbol in this file, because none of them
depends on the compiler.

## From the node

| | |
|---|---|
| **egress** | `["*"]`. Image pulls, and the public DNS resolver that dockerd needs to find a registry (finding 7). Why it cannot be narrower is in the README and in finding 3 below. An operator who packs `stack/images/*.tar` needs none of it. |
| **API slots** | One per port the packed compose file publishes, plus TCP 9000 for the health slot. The compose file's `ports:` bind on the microVM's own interface, which *is* the service's address, so a slot maps to a published port directly with no proxy. `service/compose_spec.py` refuses to start if the two lists disagree. |
| **CPU** | Whatever the operator grants, and it is shared by every container in the stack. |
| **memory** | 1 GB at init, 4 GB at most. dockerd + containerd idle at ~120 MB before the stack's own containers. |
| **disk** | 4 GB, fixed at boot from `at_init.disk_space`. **The stack's images live on this disk** — see below. |
| **a writable rootfs** | Non-negotiable, and the reason this service must **not** declare `read_only_filesystem: true`. See finding 2. |
| **cgroups** | The unified hierarchy at `/sys/fs/cgroup`. nodo's initramfs already mounts it. See finding 5. |

## From the host

**KVM, and a guest kernel built from this checkout's fragment.** Nothing else — no
device node, no module, no native application. The kernel features are all present
except the ones in findings 1 and 8. On an **x86_64** node, finding 8 would stop each
container that is not privileged; the entrypoint works around it with cgroup v1, with
no memory limits for the containers (finding 8 b).

**Not a GPU.** `celaut.Sysresources` has `mem_limit`, `disk_space`, `cpu_period`,
`cpu_quota` and `blkio_weight` and no accelerator field, so a stack needing one could
not be scheduled anywhere on this network. The same conclusion `remote-browser` and
`yt-transcript` reached.

### Why the resources are what they are

`at_init.disk_space` is not a guess and it is not headroom for this image. The exported
filesystem is **393 MB** (measured, `docker buildx build -o type=tar`), of which
dockerd's own binaries are the bulk:

| | |
|---|---|
| `dockerd` | 95 MB |
| `docker` (CLI) | 42 MB |
| `containerd` | 36 MB |
| `docker-compose` | 30 MB |
| `runc` | 14 MB |
| `containerd-shim-runc-v2` | 8.1 MB |
| Python 3.11 stdlib | 28 MB |
| `docker-proxy` + `docker-init` | 3.3 MB |
| this service's own code | 61 kB |

The remaining ~3.6 GB of the declared 4 GB is for **the operator's stack**: every image
it pulls is written into `/var/lib/docker` on this instance's disk, and a single
application image is routinely 200 MB–1.5 GB. A three-service stack on `vfs` (finding 2)
can be several times that again, because vfs copies every layer whole instead of sharing
it.

Re-checked: **`at_most.disk_space` does not grow the disk.** `requested_disk_space_bytes`
(`src/virtualizers/microvm/limits.py`) reads `at_init.disk_space` first, and the ext4
image is built once at that size. An earlier version declared 12 GB `at_most`, which
gave the stack no extra byte and only raised the admission figure. Both are now 4 GB. A
stack of large images needs `at_init.disk_space` raised, a one-line edit in
`.service/service.json`.

---

## 1. The guest kernel has `nf_tables` with no address family, so Debian's default `iptables` cannot work

This is the finding that decided a line of `service/entrypoint.sh`, and it is the one
that would otherwise produce a stack whose containers have an address and no route to
anything.

`bash/guest-kernel/nodo-guest.config:102-104` requests the nf_tables NAT path:

```
CONFIG_NF_TABLES=y
CONFIG_NFT_NAT=y
CONFIG_NFT_MASQ=y
```

In the resolved `.config`, **`CONFIG_NFT_NAT` is not there.** Kconfig dropped it, and
`net/netfilter/Kconfig` says why:

```
config NFT_NAT
	depends on NF_CONNTRACK
	select NF_NAT
	depends on NF_TABLES_IPV4 || NF_TABLES_IPV6
```

`CONFIG_NF_TABLES_IPV4`, `CONFIG_NF_TABLES_IPV6` and `CONFIG_NF_TABLES_INET` are all
**absent** from the resolved config — they are `bool`s inside `if NF_TABLES` in
`net/ipv4/netfilter/Kconfig:26-29` and nothing requests them, so `olddefconfig` leaves
them off. `NFT_NAT`'s dependency is therefore unmet and it is silently dropped. The
resolved config's complete nf_tables surface is two symbols:

```
CONFIG_NF_TABLES=y
CONFIG_NFT_MASQ=y
```

So the guest has the nf_tables *framework* and **no address family to create a table
in**. `nft add table ip nat` cannot work. Meanwhile the legacy path is complete:

```
CONFIG_IP_NF_IPTABLES=y      CONFIG_IP_NF_NAT=y       CONFIG_IP_NF_FILTER=y
CONFIG_IP_NF_IPTABLES_LEGACY=y                        CONFIG_IP_NF_MANGLE=y
CONFIG_IP_NF_TARGET_MASQUERADE=y                      CONFIG_NETFILTER_XT_NAT=y
```

**Why that breaks a container runtime specifically.** dockerd does not speak netlink for
this; it shells out to whatever `iptables` is on `PATH`. Debian bookworm's `iptables`
package installs *both* backends and points the alternative at **nft** — verified in the
base image this service pins:

```
$ update-alternatives --display iptables
  link currently points to /usr/sbin/iptables-nft
$ iptables --version
iptables v1.8.9 (nf_tables)
```

So the default `iptables` in a nodo guest is the one backend the guest kernel cannot
serve. dockerd would fail to write its NAT rules, and the symptom is not a kernel error —
it is containers that start fine and cannot reach anything off `docker0`.

**What this service does about it:** `service/entrypoint.sh` symlinks
`iptables`/`ip6tables` and their `-save`/`-restore` forms to the **legacy** binaries in
`/usr/local/sbin`, which is first on `PATH`, and logs which backend it selected.
`IPTABLES_BACKEND=nft` overrides it for a host whose kernel is the other way round.

**What nodo could do about it:** add `CONFIG_NF_TABLES_IPV4=y`, `CONFIG_NF_TABLES_IPV6=y`
and `CONFIG_NF_TABLES_INET=y` to `bash/guest-kernel/nodo-guest.config`, which would make
`CONFIG_NFT_NAT` resolvable and let either backend work. Note also that
`bash/guest-kernel/build.sh:102-107` asserts `CONFIG_NF_NAT` is `y` but **not**
`CONFIG_NFT_NAT`, which is why this dropped symbol has been invisible: the fragment asks
for it, the build does not check it, and the resolved config does not have it. Adding it
to that `assert_config` loop would turn this class of silent drop into a build failure —
which is what the loop's own comment says it is for ("A fragment entry is a request, not
a guarantee").

Also absent, and worth knowing though this service does not need them:
`CONFIG_NFT_COMPAT` (needs `NETFILTER_XTABLES`, which *is* set — but nothing requests
`NFT_COMPAT`), `CONFIG_NETFILTER_XT_TARGET_REDIRECT` and `CONFIG_NF_NAT_REDIRECT` (a
stack using `docker run -p` with a redirect-based proxy mode), `CONFIG_VXLAN` (swarm
overlay networks — irrelevant, a single-node stack has none), `CONFIG_DUMMY`,
`CONFIG_IPVLAN`, `CONFIG_NET_CLS_CGROUP`, and `CONFIG_CFS_BANDWIDTH`.

That last one has a consequence worth stating: **without `CONFIG_CFS_BANDWIDTH`, a
per-container CPU limit inside the stack is not enforceable.** `CONFIG_FAIR_GROUP_SCHED`
is set, so cpu *shares* work, but `cpu.max` does not exist, so `cpus:` or
`cpu_quota` in the operator's compose file is silently not applied. The instance as a
whole is still limited by the node, so this is a fairness question inside the stack
rather than an escape.

**Everything else dockerd needs is present on arm64**, and it is worth recording as
verified rather than assumed. On **x86_64**, six symbols of this list are absent:
`CONFIG_CGROUP_BPF`, `CONFIG_BPF_SYSCALL`, `CONFIG_BRIDGE_VLAN_FILTERING`,
`CONFIG_MACVLAN`, `CONFIG_IP_VS` and `CONFIG_IP6_NF_NAT` (not in the list, but present
on arm64). The first two are finding 8. The others are not used by a single-node stack
on the default bridge network. The list: `CONFIG_OVERLAY_FS`, `CONFIG_NAMESPACES`, `CONFIG_NET_NS`,
`CONFIG_USER_NS`, `CONFIG_PID_NS`, `CONFIG_IPC_NS`, `CONFIG_UTS_NS`, `CONFIG_CGROUPS`,
`CONFIG_CGROUP_PIDS`, `CONFIG_CGROUP_DEVICE`, `CONFIG_CGROUP_FREEZER`,
`CONFIG_CGROUP_SCHED`, `CONFIG_CGROUP_CPUACCT`, `CONFIG_MEMCG`, `CONFIG_BLK_CGROUP`,
`CONFIG_CGROUP_BPF`, `CONFIG_BPF_SYSCALL` (cgroup-v2 device control is eBPF, so this is
what replaces the v1 devices controller), `CONFIG_SECCOMP`, `CONFIG_SECCOMP_FILTER`,
`CONFIG_KEYS`, `CONFIG_VETH`, `CONFIG_BRIDGE`, `CONFIG_BRIDGE_NETFILTER`,
`CONFIG_BRIDGE_VLAN_FILTERING`, `CONFIG_NF_CONNTRACK`, `CONFIG_NF_NAT`,
`CONFIG_NF_NAT_MASQUERADE`, `CONFIG_NETFILTER_XT_MARK`,
`CONFIG_NETFILTER_XT_MATCH_ADDRTYPE`, `CONFIG_NETFILTER_XT_MATCH_CONNTRACK`,
`CONFIG_NETFILTER_XT_TARGET_MASQUERADE`, `CONFIG_POSIX_MQUEUE`, `CONFIG_MACVLAN`,
`CONFIG_IP_VS`, `CONFIG_EXT4_FS`, `CONFIG_FUSE_FS`, `CONFIG_TMPFS_XATTR`,
`CONFIG_TMPFS_POSIX_ACL`, `CONFIG_IPV6`, `CONFIG_IP6_NF_IPTABLES`.

## 2. A `read_mode=ro` service cannot run a container runtime, and the spec has no way to say so

dockerd needs a writable `/var/lib/docker` — that is where images, layers and container
filesystems go. Finding where a nodo service may write, and how much, decided
`config.DEFAULT_DATA_ROOT`.

**`/tmp` is a tmpfs, so it is memory, not disk.** `bash/build_ch_initramfs.sh:355`
mounts it that way on the read-only path and the writable path inherits `/tmp` from the
rootfs image. A data-root under `/tmp` would charge every image layer the stack pulls
against the instance's **`mem_limit`**, so a 1.5 GB image on a 1 GB instance would not
fill a disk — it would OOM the guest, and `disk_space` in the manifest would describe
nothing the service uses. So the data-root is `/var/lib/docker`, on the rootfs.

**On the ordinary (writable) path that works, and the sizing is already correct.**
`src/virtualizers/microvm/limits.py:437-441`:

```python
return max(
    MIN_ROOTFS_BYTES,
    int(total_bytes) + OVERHEAD_BYTES,
    int(requested_disk_space_bytes(service) or 0),
)
```

`at_init.disk_space` is a **floor** the ext4 image is grown to, which is exactly what a
data-root needs. The 4 GB this service declares is therefore 4 GB of real, writable ext4.

**On the read-only path it is impossible, in two independent ways.** `read_mode=ro`
(`docs/PACKING.md:534`) builds the rootfs as squashfs or erofs and `/init` overlays it
with a **tmpfs** upper layer (`bash/build_ch_initramfs.sh:320-325`):

```sh
mount -t tmpfs -o mode=755,nosuid,nodev tmpfs /overlay
mount -t overlay overlay \
    -o lowerdir=/lower,upperdir=/overlay/upper,workdir=/overlay/work /newroot
```

So (a) every write outside `/tmp` lands in RAM — the comment at line 72 of that block
says so outright, "whatever the service writes outside /tmp […] is RAM the instance is
already billed for as memory rather than disk" — and (b) `at_init.disk_space` inverts
from a floor to a **ceiling** (`limits.py:423-435`, and
`assert_within_disk_space_ceiling` refuses a build above it), so there is no way to ask
for writable space at all.

There is a third, narrower consequence: dockerd's `overlay2` driver cannot put an
overlay upper directory **on** an overlayfs, so a ro service would be forced to `vfs`
even if the space existed. This service's `overlay2 → vfs` fallback handles it, and it
is why that fallback exists rather than being a nicety.

**The gap:** nothing in the spec lets a service say *"my rootfs may be immutable but I
need N bytes of writable, on-disk scratch"*. The two properties are bundled into one
boolean. `read_only_filesystem: true` is the right declaration for almost every service
in `celaut-basics` and is exactly wrong for this one, and the only way to express that
is to not use it — which also gives up the 64 MiB `OVERHEAD_BYTES` and 128 MiB
`MIN_ROOTFS_BYTES` saving that issue #369 was about. A `scratch_space` sibling to
`disk_space`, or a declared writable mount, would let a service take the immutable
rootfs *and* a data-root. This service declares no `read_only_filesystem` and says why
here rather than declaring it and failing at runtime.

## 3. A hostname tag grants addresses the guest cannot resolve

Not specific to registries — it applies to **any** service that reaches a named host,
and it is why `network` here is `["*"]`.

Declaring `tags: ["registry-1.docker.io"]` looks like the right, narrow thing. What the
node does with it:

- `resolve_network` → `resolve_domain` (`src/manager/networks.py`) resolves the tag
  **on the node**, to IPv4 A records, and builds `Instance.Uri` entries for ports 80 and
  443, or for the one port that the entry's `formal` states as `port=<n>` (#389);
- the firewall writes one allow per address, on the forward hook.

And from `src/virtualizers/microvm/network.py:513`:

> There is no rule for port 53: nodo does not serve DNS, and a guest that wants name
> resolution gets it from a service […] or inside its own container.

So the guest is granted **addresses**, over TCP, for hosts it has no way to **look up**.
dockerd resolves registry names itself and cannot be handed addresses — and a TLS client
needs the name for SNI and certificate validation regardless. The declaration reads as a
tight confinement and produces a service that cannot pull a single image.

Two more, both verified in the same file:

- **Only the first tag of an entry is used.** `resolve_network`
  (`src/manager/networks.py:266-273`) documents the tags of an entry as *synonyms* and
  `break`s at the first that resolves. A `demo-service`-style entry listing several
  registries grants the first one's addresses and silently drops the rest. The docstring
  says this is the semantics rather than an optimization — which is a defensible
  reading, but it means `docs/PACKING.md`'s examples showing two hostnames in one entry
  are showing alternates, not a set, and that is worth saying there.
- **A wildcard tag grants nothing, and no longer aborts the launch.** There is still no
  wildcard-hostname syntax — `*.docker.io` is not a name any resolver answers. What it
  does is now the right thing: #391 (merged as `306e3531`) wraps `resolve_domain` in a
  `try/except ValueError`, logs `tag '*.docker.io' is a wildcard hostname, which the
  resolver does not support`, and yields no peers, so the guest boots with default-deny
  toward it. Before that fix the `ValueError` escaped to the launcher's catch-all and
  failed the launch with a message naming a hostname nobody meant literally. Worth
  recording because `yt-transcript`'s `NODE-REQUIREMENTS.md` documents the old behaviour
  as finding 3, and on current `dev` that finding is closed.

(A tag with no dot is *skipped* silently by the `not tag.islower() or '.' not in tag`
guard instead, which is why `"*"` works: it resolves to no URIs, and
`configure_guest_firewall_policy` matches the literal `"*"` separately at
`network.py:549`.)

**What this service does instead:** declares `["*"]`, says why in the `prose`, and gives
the operator a way to need none of it — `stack/images/*.tar` are `docker load`ed before
compose runs, which makes the image *bytes* part of the content-addressed service rather
than a digest pointing at a registry. That is strictly stronger than a narrow network
declaration would have been.

Under the operator's `service_networks` policy: a **whitelist** must list `"*"`
explicitly (`docs/NETWORKS.md` — `*` is matched as a tag, not as a glob), and
`blacklist: ["*"]` refuses this service at launch, correctly, unless its images are
packed.

## 4. Privilege cannot be declared, so it is taken

Every other service in `celaut-basics` drops to an unprivileged uid in its entrypoint.
This one cannot: dockerd needs root in its namespace to create bridges, write iptables
rules, mount overlays and clone namespaces.

**Rootless dockerd is the alternative and the spec cannot express what it needs.** It
wants `/dev/fuse` (for `fuse-overlayfs`), a uid/gid map, and `newuidmap`/`newgidmap`
setuid helpers. `celaut.Service` has no field for a device node, no field for a uid map,
and no field for "this service needs to be root" or "this service must not be". So:

- there is no way for this service to **declare** that it runs privileged, which means
  an operator cannot filter on it and a node cannot refuse it on that basis;
- and there is no way to declare the `/dev/fuse` a rootless version would need instead.

The confinement this service relies on is therefore the **microVM boundary**, not a uid
inside it. That is a weaker claim than `yt-transcript`'s "PID 1 is uid 10001", and it is
the honest one: inside its VM, this service is root, and the stack's containers are
whatever the operator's compose file says. Under the microVM virtualizer that is
defensible — the VM is the boundary, and a privileged process inside a KVM guest is not
privileged on the host. **Under a container-based backend it would not be**, and
`docs/BACKENDS.md` should probably say that a service like this one is microVM-only.
This service cannot express that either.

## 5. The cgroup mount a container runtime needs is best-effort

`bash/build_ch_initramfs.sh:374-375`:

```sh
mount -t cgroup2 none /newroot/sys/fs/cgroup \
    || log "warning: could not mount cgroup2 at /sys/fs/cgroup (container-runtime services may fail)"
```

The comment above it names this exact class of service and the exact failure: without a
cgroup mount, rootful dockerd defaults to legacy cgroup-v1, finds no controllers, and
aborts with "Devices cgroup isn't mounted" → PID 1 exits → kernel panic.

Non-fatal is the right choice for `/init` — a service that does not use cgroups should
not fail to boot over it. But it means a `compose-runner` instance on a host where that
mount failed gets a kernel panic with the *real* reason in a warning line well above it.
`service/entrypoint.sh` checks for the mount, reports which version is present, and
attempts a `cgroup2` mount if nothing is there, so the log says what happened before
dockerd's own complaint.

No change requested: a service-side check is the correct place for a service-specific
requirement. Recorded because the distance between the warning and the panic is what
makes it hard to diagnose.

---

## 6. nodo applies no `ENV` of the image, so `PATH` is the entrypoint's job

Re-checked against `b2f79449`. The packer exports the built image as a filesystem
(`buildctl build --output type=tar`, `docs/PACKING.md`). Image metadata, `ENV` included,
is not kept. The guest `/init` (`bash/build_ch_initramfs.sh`) exports its own
`PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin` and then does
`exec switch_root /newroot "$ENTRYPOINT"`.

**Why that breaks a container runtime specifically.** The Docker static bundle is in
`/opt/docker/bin`, and dockerd finds `containerd`, `runc`, `docker-proxy` and the shim
through `PATH`. The Dockerfile's `ENV PATH=…/opt/docker/bin…` made every
`docker run` test pass, and has no effect under nodo. dockerd would start and fail to
start containerd.

**What this service does about it:** `service/entrypoint.sh` exports `PATH`
(`config.RUNTIME_PATH`), and the supervisor sets it again for each child.
`tests/test_image.sh` starts the container with the `/init` PATH, so a local run
tests the same environment as a node.

**What nodo could do about it:** `docs/PACKING.md` already says not to use `ENV` for
runtime configuration. A line that says `ENV PATH` is lost too would help, because
`PATH` is the one variable a Dockerfile usually sets for its own binaries.

## 7. A guest has no resolver, and `["*"]` does not give one

Re-checked against `b2f79449`. `src/virtualizers/microvm/network.py` states that nodo
writes no `/etc/hosts` and no `/etc/resolv.conf` into the guest, serves no DNS, and
opens no port 53 toward the node. The `*` tag opens egress to every address, but the
node does not name a resolver.

The pinned `debian:bookworm-slim` layer **does** ship `/etc/resolv.conf` as a regular
file with `nameserver 1.1.1.1` and `nameserver 1.0.0.1`. That was read from `docker
save` of the index digest in `.service/Dockerfile`, not from `docker run` (the engine
replaces the file at create). Whether `nodo pack` keeps that file in the exported tar
is not proven. If the packed rootfs has no nameserver, dockerd fails each pull with a
lookup error.

**What this service does about it:** before dockerd starts, the supervisor writes
`/etc/resolv.conf` with `DNS_SERVERS` (default `1.1.1.1 8.8.8.8`) when the file names no
server. The `*` grant makes those addresses reachable. A file that already names a
server (the debian layer, or the `docker run` mount) is kept, unless `DNS_SERVERS` is
set.

**The gap:** the spec has no way to ask the node for a resolver, and no way to say
which resolver a `*` service uses. A service that declares `*` and takes host names
must bring its own, as this one does.

## 8. The guest kernel misses two things that runc and dockerd 28+ use

Found by the audit against `698e6583`, by Kconfig resolution of both architectures (see
the top of this file). Neither is in an earlier finding, and the runs under
`docker run --privileged` could not show them, because the host kernel had both.

**a. No `raw` table (`CONFIG_IP_NF_RAW`), on arm64 and on x86_64.** Nothing in
`bash/guest-kernel/nodo-guest.config:98-112` asks for it, and neither `defconfig` sets
it. dockerd 28.0 and later writes a DROP rule in the `raw` table for each container
endpoint on a bridge network. In moby `docker-v29.8.1`, the rule is in
`daemon/libnetwork/drivers/bridge/internal/iptabler/endpoint.go`
(`filterDirectAccess`), and `appendOrDelChainRule` returns the iptables error. So with
no `raw` table, each container of the stack fails to start. The only switch is the
environment variable `DOCKER_INSECURE_NO_IPTABLES_RAW=1`
(`iptabler/port.go`, `rawRulesDisabled`).

**What this service does about it:** the supervisor reads the `raw` table with the
selected `iptables` (`iptables -t raw -S`). If that fails, it starts dockerd with
`DOCKER_INSECURE_NO_IPTABLES_RAW=1` and logs a warning. A value set at launch is kept.
The cost: dockerd does not block packets that a host on the same link sends straight to
a container address. In a nodo guest that link is the node's bridge, and the node
firewall blocks traffic between instances (`src/virtualizers/microvm/network.py`,
`configure_guest_firewall_policy`, default deny).

**b. No `bpf(2)` on x86_64 (`CONFIG_BPF_SYSCALL`, and so no `CONFIG_CGROUP_BPF`).**
The x86_64 `defconfig` of linux 6.12.103 sets no BPF option, and the fragment asks for
none. The arm64 `defconfig` sets `CONFIG_BPF_SYSCALL=y`, so the arm64 guest has it. On
cgroup v2, which nodo's `/init` mounts (`bash/build_ch_initramfs.sh:373-375`), runc
applies the device rules of a container with an eBPF program. If the program cannot be
loaded, runc refuses the container unless every rule allows everything
(`opencontainers/cgroups`, `devices/v2.go`, `setV2` and `canSkipEBPFError`). A default
container has deny rules. So on an x86_64 node, each container that is not
`privileged: true` fails to start.

**What this service does** (since 2026-10-06): `service/entrypoint.sh` step 3b. When
cgroup v2 is mounted and `/proc/sys/kernel/unprivileged_bpf_disabled` is missing (that
sysctl exists only with `CONFIG_BPF_SYSCALL`), it unmounts cgroup v2 and mounts one
cgroup v1 hierarchy for each controller that the kernel can mount on v1. The v1
devices controller needs no eBPF, so runc applies the device rules there. The guest has
no `CONFIG_MEMCG_V1`, so v1 has no memory controller: the memory limits of the stack's
containers are not enforced, and the entrypoint logs a warning. The microVM keeps its
own memory limit. If the devices controller cannot be mounted on v1, the entrypoint
mounts cgroup v2 again and logs a warning. The supervisor still logs its warning if
cgroup v2 stays mounted with no bpf(2).

**What nodo could do about it:** add `CONFIG_BPF_SYSCALL=y`, `CONFIG_CGROUP_BPF=y` and
`CONFIG_IP_NF_RAW=y` to `bash/guest-kernel/nodo-guest.config`, in the block that says
"Containers inside the guest: a service may boot its own dockerd", and add them to the
`assert_config` loop of `bash/guest-kernel/build.sh`. That loop does not check
`CONFIG_NFT_NAT` either (finding 1).

**Verified on a real node** (nodo `dev` `f14a1447`, x86_64 with KVM, 2026-10-06): the
pinned x86_64 guest kernel (6.12.103) has no `unprivileged_bpf_disabled` string in its
decompressed image; the arm64 one has it. With step 3b, the `linux/amd64` tree booted,
mounted v1 for `devices cpu cpuacct pids freezer blkio net_cls perf_event hugetlb` (no
`memory`, no `cpuset`), and brought the example stack up (two unprivileged containers,
images pulled from Docker Hub) in 98 s.

**Fixed in nodo** (2026-10-07): the guest kernel config now sets `CONFIG_BPF_SYSCALL`,
`CONFIG_CGROUP_BPF` and `CONFIG_IP_NF_RAW` (celaut-project/nodo#504, closed), and
celaut-project/nodo#509 pins the rebuilt guest. Real node run on nodo `dev` `7dc267a2`
(x86_64 with KVM, 2026-10-08), released `v1` (`a87faa4a…`): the entrypoint kept cgroup v2,
the supervisor logged no `raw` warning, the stack was up in 114.5 s, `/health` gave 200
with 2 of 2 containers, `:8080/` gave 200 from whoami, and port 6379 refused the
connection. The workarounds of a and b stay for nodes with an older guest kernel.

## Also re-checked: how an instance stops

`nodo kill` sends SIGKILL to the hypervisor process (`src/virtualizers/microvm/kill.py`)
and removes the runtime directory with the guest disk. The guest gets no signal, so the
supervisor's SIGTERM handler, and `docker compose down`, do not run on a node. That is
not a defect here: nothing on the guest disk survives the instance. The handler is for
`docker stop` and other runtimes.

If the supervisor exits, PID 1 (tini) exits and the guest kernel panics. nodo reads the
panic line from the serial log (`src/virtualizers/microvm/guest_panic.py`) and removes
the instance. So a fatal error in the supervisor is an exit with a logged reason.

## What was verified, and where

Everything in findings 1–5 was read from the checkout at `33ebc154` and, where it is a
kernel claim, resolved with Kconfig rather than read off the fragment. **What was not
verified is the whole of it in place: this has never been launched under a real nodo.**
The runtime behaviour below was verified under `docker run --privileged` on
linux/arm64 — which exercises dockerd, compose, the bridge, the published ports and the
teardown, but not nodo's initramfs, its firewall, or a real microVM. See the README's
"What was verified by running it".

All citations above are to `celaut-project/nodo` at `upstream/dev` = `306e3531`. The
guest kernel fragment, `bash/guest-kernel/build.sh`, `bash/build_ch_initramfs.sh`,
`src/virtualizers/microvm/limits.py` and `src/virtualizers/microvm/network.py` are
byte-identical between that commit and the working checkout this was developed against;
the only file that differs is `src/manager/networks.py`, and the difference is the #391
wildcard fix noted in finding 3.
