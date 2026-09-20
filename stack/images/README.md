# `stack/images/` — the offline option

Drop `docker save` output here and this service needs **no egress at all**.

`service/supervisor.py` runs `docker load --input` on every `*.tar` in this directory, in
sorted order, before `docker compose up` — so the images are already in the inner
daemon's store and compose never contacts a registry.

```sh
docker pull myapp:1.2.3
docker save myapp:1.2.3 -o stack/images/myapp.tar
# then make sure the compose file names exactly `myapp:1.2.3`
nodo pack .
```

Two things worth knowing:

**This is stronger than a digest in the compose file, not just more convenient.** A
digest *points at* a registry; a tar packed here *is* part of the content-addressed
service — the image bytes are hashed into the service id along with everything else in
`stack/`. Nobody has to be reachable, or trusted, at launch.

**It costs disk twice.** The tar sits in the exported filesystem and `docker load`
expands it into `/var/lib/docker` at startup, so budget roughly two copies of every image
in `at_init.disk_space` in `.service/service.json`. On `vfs` (see
[`NODE-REQUIREMENTS.md`](../../NODE-REQUIREMENTS.md) finding 2) expansion costs more
again.

A tar that fails to load is **fatal**, deliberately: an image the operator packed and the
service silently skipped would fail later as a pull against a registry they may have
declared no egress to.

This file is here so the directory exists in git. It is ignored by the loader, which only
reads `*.tar`.
